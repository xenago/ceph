import hashlib
import json
import logging
import uuid
from collections import defaultdict
from typing import TYPE_CHECKING, Optional, List, cast, Dict, Any, Union, Tuple, Set

from ceph.deployment import inventory
from ceph.deployment.drive_group import DriveGroupSpec
from ceph.deployment.service_spec import ServiceSpec, CustomContainerSpec, PlacementSpec
from ceph.utils import datetime_now

import orchestrator
from orchestrator import OrchestratorError, set_exception_subject, OrchestratorEvent, \
    DaemonDescriptionStatus, daemon_type_to_service
from cephadm.services.cephadmservice import CephadmDaemonDeploySpec
from cephadm.schedule import HostAssignment
from cephadm.autotune import MemoryAutotuner
from cephadm.utils import forall_hosts, cephadmNoImage, is_repo_digest, \
    CephadmNoImage, CEPH_TYPES, ContainerInspectInfo
from mgr_module import MonCommandFailed
from mgr_util import format_bytes

from . import utils

if TYPE_CHECKING:
    from cephadm.module import CephadmOrchestrator

logger = logging.getLogger(__name__)

REQUIRES_POST_ACTIONS = ['grafana', 'iscsi', 'prometheus', 'alertmanager', 'rgw']


class CephadmServe:
    """
    This module contains functions that are executed in the
    serve() thread. Thus they don't block the CLI.

    Please see the `Note regarding network calls from CLI handlers`
    chapter in the cephadm developer guide.

    On the other hand, These function should *not* be called form
    CLI handlers, to avoid blocking the CLI
    """

    def __init__(self, mgr: "CephadmOrchestrator"):
        self.mgr: "CephadmOrchestrator" = mgr
        self.log = logger

    def serve(self) -> None:
        """
        The main loop of cephadm.

        A command handler will typically change the declarative state
        of cephadm. This loop will then attempt to apply this new state.
        """
        self.log.debug("serve starting")
        self.mgr.config_checker.load_network_config()

        while self.mgr.run:

            try:

                self.convert_tags_to_repo_digest()

                # refresh daemons
                self.log.debug('refreshing hosts and daemons')
                self._refresh_hosts_and_daemons()

                self._check_for_strays()

                self._update_paused_health()

                if self.mgr.need_connect_dashboard_rgw and self.mgr.config_dashboard:
                    self.mgr.need_connect_dashboard_rgw = False
                    if 'dashboard' in self.mgr.get('mgr_map')['modules']:
                        self.log.info('Checking dashboard <-> RGW credentials')
                        self.mgr.remote('dashboard', 'set_rgw_credentials')

                if not self.mgr.paused:
                    self.mgr.to_remove_osds.process_removal_queue()

                    self.mgr.migration.migrate()
                    if self.mgr.migration.is_migration_ongoing():
                        continue

                    if self._apply_all_services():
                        continue  # did something, refresh

                    self._check_daemons()

                    self._purge_deleted_services()

                    if self.mgr.use_agent:
                        # on the off chance there are still agents hanging around from
                        # when we turned the config option off, we need to redeploy them
                        # we can tell they're in that state if we don't have a keyring for
                        # them in the host cache
                        for agent in self.mgr.cache.get_daemons_by_service('agent'):
                            if agent.hostname not in self.mgr.cache.agent_keys:
                                self.mgr._schedule_daemon_action(agent.name(), 'redeploy')
                        if 'agent' not in self.mgr.spec_store:
                            self.mgr.agent_helpers._apply_agent()
                    else:
                        if 'agent' in self.mgr.spec_store:
                            self.mgr.spec_store.rm('agent')
                        self.mgr.cache.agent_counter = {}
                        self.mgr.cache.agent_timestamp = {}
                        self.mgr.cache.agent_keys = {}
                        self.mgr.cache.agent_ports = {}

                    if self.mgr.upgrade.continue_upgrade():
                        continue

            except OrchestratorError as e:
                if e.event_subject:
                    self.mgr.events.from_orch_error(e)

            self._serve_sleep()
        self.log.debug("serve exit")

    def _serve_sleep(self) -> None:
        sleep_interval = max(
            30,
            min(
                self.mgr.host_check_interval,
                self.mgr.facts_cache_timeout,
                self.mgr.daemon_cache_timeout,
                self.mgr.device_cache_timeout,
            )
        )
        self.log.debug('Sleeping for %d seconds', sleep_interval)
        self.mgr.event.wait(sleep_interval)
        self.mgr.event.clear()

    def _update_paused_health(self) -> None:
        if self.mgr.paused:
            self.mgr.health_checks['CEPHADM_PAUSED'] = {
                'severity': 'warning',
                'summary': 'cephadm background work is paused',
                'count': 1,
                'detail': ["'ceph orch resume' to resume"],
            }
            self.mgr.set_health_checks(self.mgr.health_checks)
        else:
            if 'CEPHADM_PAUSED' in self.mgr.health_checks:
                del self.mgr.health_checks['CEPHADM_PAUSED']
                self.mgr.set_health_checks(self.mgr.health_checks)

    def _autotune_host_memory(self, host: str) -> None:
        total_mem = self.mgr.cache.get_facts(host).get('memory_total_kb', 0)
        if not total_mem:
            val = None
        else:
            total_mem *= 1024   # kb -> bytes
            total_mem *= self.mgr.autotune_memory_target_ratio
            a = MemoryAutotuner(
                daemons=self.mgr.cache.get_daemons_by_host(host),
                config_get=self.mgr.get_foreign_ceph_option,
                total_mem=total_mem,
            )
            val, osds = a.tune()
            any_changed = False
            for o in osds:
                if self.mgr.get_foreign_ceph_option(o, 'osd_memory_target') != val:
                    self.mgr.check_mon_command({
                        'prefix': 'config rm',
                        'who': o,
                        'name': 'osd_memory_target',
                    })
                    any_changed = True
        if val is not None:
            if any_changed:
                self.mgr.log.info(
                    f'Adjusting osd_memory_target on {host} to {format_bytes(val, 6)}'
                )
                ret, out, err = self.mgr.mon_command({
                    'prefix': 'config set',
                    'who': f'osd/host:{host}',
                    'name': 'osd_memory_target',
                    'value': str(val),
                })
                if ret:
                    self.log.warning(
                        f'Unable to set osd_memory_target on {host} to {val}: {err}'
                    )
        else:
            self.mgr.check_mon_command({
                'prefix': 'config rm',
                'who': f'osd/host:{host}',
                'name': 'osd_memory_target',
            })
        self.mgr.cache.update_autotune(host)

    def _refresh_hosts_and_daemons(self) -> None:
        bad_hosts = []
        failures = []

        # host -> path -> (mode, uid, gid, content, digest)
        client_files: Dict[str, Dict[str, Tuple[int, int, int, bytes, str]]] = {}

        # ceph.conf
        if self.mgr.manage_etc_ceph_ceph_conf or self.mgr.keys.keys:
            config = self.mgr.get_minimal_ceph_conf().encode('utf-8')
            config_digest = ''.join('%02x' % c for c in hashlib.sha256(config).digest())

        if self.mgr.manage_etc_ceph_ceph_conf:
            try:
                pspec = PlacementSpec.from_string(self.mgr.manage_etc_ceph_ceph_conf_hosts)
                ha = HostAssignment(
                    spec=ServiceSpec('mon', placement=pspec),
                    hosts=self.mgr.cache.get_schedulable_hosts(),
                    unreachable_hosts=self.mgr.cache.get_unreachable_hosts(),
                    daemons=[],
                    networks=self.mgr.cache.networks,
                )
                all_slots, _, _ = ha.place()
                for host in {s.hostname for s in all_slots}:
                    if host not in client_files:
                        client_files[host] = {}
                    client_files[host]['/etc/ceph/ceph.conf'] = (
                        0o644, 0, 0, bytes(config), str(config_digest)
                    )
            except Exception as e:
                self.mgr.log.warning(
                    f'unable to calc conf hosts: {self.mgr.manage_etc_ceph_ceph_conf_hosts}: {e}')

        # client keyrings
        for ks in self.mgr.keys.keys.values():
            assert config
            assert config_digest
            try:
                ret, keyring, err = self.mgr.mon_command({
                    'prefix': 'auth get',
                    'entity': ks.entity,
                })
                if ret:
                    self.log.warning(f'unable to fetch keyring for {ks.entity}')
                    continue
                digest = ''.join('%02x' % c for c in hashlib.sha256(
                    keyring.encode('utf-8')).digest())
                ha = HostAssignment(
                    spec=ServiceSpec('mon', placement=ks.placement),
                    hosts=self.mgr.cache.get_schedulable_hosts(),
                    unreachable_hosts=self.mgr.cache.get_unreachable_hosts(),
                    daemons=[],
                    networks=self.mgr.cache.networks,
                )
                all_slots, _, _ = ha.place()
                for host in {s.hostname for s in all_slots}:
                    if host not in client_files:
                        client_files[host] = {}
                    client_files[host]['/etc/ceph/ceph.conf'] = (
                        0o644, 0, 0, bytes(config), str(config_digest)
                    )
                    client_files[host][ks.path] = (
                        ks.mode, ks.uid, ks.gid, keyring.encode('utf-8'), digest
                    )
            except Exception as e:
                self.log.warning(
                    f'unable to calc client keyring {ks.entity} placement {ks.placement}: {e}')

        agents_down: List[str] = []

        @forall_hosts
        def refresh(host: str) -> None:

            # skip hosts that are in maintenance - they could be powered off
            if self.mgr.inventory._inventory[host].get("status", "").lower() == "maintenance":
                return

            if self.mgr.use_agent and self.mgr.agent_helpers._agent_down(host):
                agents_down.append(host)
                self.mgr.cache.metadata_up_to_date[host] = False
                if host in self.mgr.offline_hosts:
                    return

                # In case host is actually offline, it's best to reset the connection to avoid
                # a long timeout trying to use an existing connection to an offline host
                # REVISIT AFTER https://github.com/ceph/ceph/pull/42919
                # self.mgr.ssh._reset_con(host)

                try:
                    # try to schedule redeploy of agent in case it is individually down
                    agent = [a for a in self.mgr.cache.get_daemons_by_host(
                        host) if a.hostname == host][0]
                    self.mgr._schedule_daemon_action(agent.name(), 'redeploy')
                except Exception as e:
                    self.log.debug(
                        f'Failed to find entry for agent deployed on host {host}. Agent possibly never deployed: {e}')
                return

            if self.mgr.cache.host_needs_check(host):
                r = self._check_host(host)
                if r is not None:
                    bad_hosts.append(r)

            if not self.mgr.use_agent or host not in [h.hostname for h in self.mgr.cache.get_non_draining_hosts()]:
                if self.mgr.cache.host_needs_daemon_refresh(host):
                    self.log.debug('refreshing %s daemons' % host)
                    r = self._refresh_host_daemons(host)
                    if r:
                        failures.append(r)

                if self.mgr.cache.host_needs_facts_refresh(host):
                    self.log.debug(('Refreshing %s facts' % host))
                    r = self._refresh_facts(host)
                    if r:
                        failures.append(r)

                if self.mgr.cache.host_needs_network_refresh(host):
                    self.log.debug(('Refreshing %s networks' % host))
                    r = self._refresh_host_networks(host)
                    if r:
                        failures.append(r)

                if self.mgr.cache.host_needs_device_refresh(host):
                    self.log.debug('refreshing %s devices' % host)
                    r = self._refresh_host_devices(host)
                    if r:
                        failures.append(r)
                self.mgr.cache.metadata_up_to_date[host] = True

            if self.mgr.cache.host_needs_registry_login(host) and self.mgr.registry_url:
                self.log.debug(f"Logging `{host}` into custom registry")
                r = self._registry_login(host, self.mgr.registry_url,
                                         self.mgr.registry_username, self.mgr.registry_password)
                if r:
                    bad_hosts.append(r)

            if self.mgr.cache.host_needs_osdspec_preview_refresh(host):
                self.log.debug(f"refreshing OSDSpec previews for {host}")
                r = self._refresh_host_osdspec_previews(host)
                if r:
                    failures.append(r)

            if (
                    self.mgr.cache.host_needs_autotune_memory(host)
                    and not self.mgr.inventory.has_label(host, '_no_autotune_memory')
            ):
                self.log.debug(f"autotuning memory for {host}")
                self._autotune_host_memory(host)

            # client files
            updated_files = False
            old_files = self.mgr.cache.get_host_client_files(host).copy()
            for path, m in client_files.get(host, {}).items():
                mode, uid, gid, content, digest = m
                if path in old_files:
                    match = old_files[path] == (digest, mode, uid, gid)
                    del old_files[path]
                    if match:
                        continue
                self.log.info(f'Updating {host}:{path}')
                self.mgr.ssh.write_remote_file(host, path, content, mode, uid, gid)
                self.mgr.cache.update_client_file(host, path, digest, mode, uid, gid)
                updated_files = True
            for path in old_files.keys():
                self.log.info(f'Removing {host}:{path}')
                cmd = ['rm', '-f', path]
                self.mgr.ssh.check_execute_command(host, cmd)
                updated_files = True
                self.mgr.cache.removed_client_file(host, path)
            if updated_files:
                self.mgr.cache.save_host(host)

        refresh(self.mgr.cache.get_hosts())

        if 'CEPHADM_AGENT_DOWN' in self.mgr.health_checks:
            del self.mgr.health_checks['CEPHADM_AGENT_DOWN']
        if agents_down:
            detail: List[str] = []
            for agent in agents_down:
                detail.append((f'Cephadm agent on host {agent} has not reported in '
                              f'{2.5 * self.mgr.agent_refresh_rate} seconds. Agent is assumed '
                               'down and host may be offline.'))
            self.mgr.health_checks['CEPHADM_AGENT_DOWN'] = {
                'severity': 'warning',
                'summary': '%d Cephadm Agent(s) are not reporting. '
                'Hosts may be offline' % (len(agents_down)),
                'count': len(agents_down),
                'detail': detail,
            }
            self.mgr.set_health_checks(self.mgr.health_checks)

        self.mgr.config_checker.run_checks()

        health_changed = False
        for k in [
                'CEPHADM_HOST_CHECK_FAILED',
                'CEPHADM_FAILED_DAEMON',
                'CEPHADM_REFRESH_FAILED',
        ]:
            if k in self.mgr.health_checks:
                del self.mgr.health_checks[k]
                health_changed = True
        if bad_hosts:
            self.mgr.health_checks['CEPHADM_HOST_CHECK_FAILED'] = {
                'severity': 'warning',
                'summary': '%d hosts fail cephadm check' % len(bad_hosts),
                'count': len(bad_hosts),
                'detail': bad_hosts,
            }
            health_changed = True
        if failures:
            self.mgr.health_checks['CEPHADM_REFRESH_FAILED'] = {
                'severity': 'warning',
                'summary': 'failed to probe daemons or devices',
                'count': len(failures),
                'detail': failures,
            }
            health_changed = True
        failed_daemons = []
        for dd in self.mgr.cache.get_daemons():
            if dd.status is not None and dd.status == DaemonDescriptionStatus.error:
                failed_daemons.append('daemon %s on %s is in %s state' % (
                    dd.name(), dd.hostname, dd.status_desc
                ))
        if failed_daemons:
            self.mgr.health_checks['CEPHADM_FAILED_DAEMON'] = {
                'severity': 'warning',
                'summary': '%d failed cephadm daemon(s)' % len(failed_daemons),
                'count': len(failed_daemons),
                'detail': failed_daemons,
            }
            health_changed = True
        if health_changed:
            self.mgr.set_health_checks(self.mgr.health_checks)

    def _check_host(self, host: str) -> Optional[str]:
        if host not in self.mgr.inventory:
            return None
        self.log.debug(' checking %s' % host)
        try:
            addr = self.mgr.inventory.get_addr(host) if host in self.mgr.inventory else host
            out, err, code = self._run_cephadm(
                host, cephadmNoImage, 'check-host', [],
                error_ok=True, no_fsid=True)
            self.mgr.cache.update_last_host_check(host)
            self.mgr.cache.save_host(host)
            if code:
                self.log.debug(' host %s (%s) failed check' % (host, addr))
                if self.mgr.warn_on_failed_host_check:
                    return 'host %s (%s) failed check: %s' % (host, addr, err)
            else:
                self.log.debug(' host %s (%s) ok' % (host, addr))
        except Exception as e:
            self.log.debug(' host %s (%s) failed check' % (host, addr))
            return 'host %s (%s) failed check: %s' % (host, addr, e)
        return None

    def _refresh_host_daemons(self, host: str) -> Optional[str]:
        try:
            ls = self._run_cephadm_json(host, 'mon', 'ls', [], no_fsid=True)
        except OrchestratorError as e:
            return str(e)
        self.mgr._process_ls_output(host, ls)
        return None

    def _refresh_facts(self, host: str) -> Optional[str]:
        try:
            val = self._run_cephadm_json(host, cephadmNoImage, 'gather-facts', [], no_fsid=True)
        except OrchestratorError as e:
            return str(e)

        self.mgr.cache.update_host_facts(host, val)

        return None

    def _refresh_host_devices(self, host: str) -> Optional[str]:
        with_lsm = self.mgr.get_module_option('device_enhanced_scan')
        inventory_args = ['--', 'inventory',
                          '--format=json',
                          '--filter-for-batch']
        if with_lsm:
            inventory_args.insert(-1, "--with-lsm")

        try:
            try:
                devices = self._run_cephadm_json(host, 'osd', 'ceph-volume',
                                                 inventory_args)
            except OrchestratorError as e:
                if 'unrecognized arguments: --filter-for-batch' in str(e):
                    rerun_args = inventory_args.copy()
                    rerun_args.remove('--filter-for-batch')
                    devices = self._run_cephadm_json(host, 'osd', 'ceph-volume',
                                                     rerun_args)
                else:
                    raise

        except OrchestratorError as e:
            return str(e)

        self.log.debug('Refreshed host %s devices (%d)' % (
            host, len(devices)))
        ret = inventory.Devices.from_json(devices)
        self.mgr.cache.update_host_devices(host, ret.devices)
        self.update_osdspec_previews(host)
        self.mgr.cache.save_host(host)
        return None

    def _refresh_host_networks(self, host: str) -> Optional[str]:
        try:
            networks = self._run_cephadm_json(host, 'mon', 'list-networks', [], no_fsid=True)
        except OrchestratorError as e:
            return str(e)

        self.log.debug('Refreshed host %s networks (%s)' % (
            host, len(networks)))
        self.mgr.cache.update_host_networks(host, networks)
        self.mgr.cache.save_host(host)
        return None

    def _refresh_host_osdspec_previews(self, host: str) -> Optional[str]:
        self.update_osdspec_previews(host)
        self.mgr.cache.save_host(host)
        self.log.debug(f'Refreshed OSDSpec previews for host <{host}>')
        return None

    def update_osdspec_previews(self, search_host: str = '') -> None:
        # Set global 'pending' flag for host
        self.mgr.cache.loading_osdspec_preview.add(search_host)
        previews = []
        # query OSDSpecs for host <search host> and generate/get the preview
        # There can be multiple previews for one host due to multiple OSDSpecs.
        previews.extend(self.mgr.osd_service.get_previews(search_host))
        self.log.debug(f'Loading OSDSpec previews to HostCache for host <{search_host}>')
        self.mgr.cache.osdspec_previews[search_host] = previews
        # Unset global 'pending' flag for host
        self.mgr.cache.loading_osdspec_preview.remove(search_host)

    def _check_for_strays(self) -> None:
        self.log.debug('_check_for_strays')
        for k in ['CEPHADM_STRAY_HOST',
                  'CEPHADM_STRAY_DAEMON']:
            if k in self.mgr.health_checks:
                del self.mgr.health_checks[k]
        if self.mgr.warn_on_stray_hosts or self.mgr.warn_on_stray_daemons:
            ls = self.mgr.list_servers()
            managed = self.mgr.cache.get_daemon_names()
            host_detail = []     # type: List[str]
            host_num_daemons = 0
            daemon_detail = []  # type: List[str]
            for item in ls:
                host = item.get('hostname')
                assert isinstance(host, str)
                daemons = item.get('services')  # misnomer!
                assert isinstance(daemons, list)
                missing_names = []
                for s in daemons:
                    daemon_id = s.get('id')
                    assert daemon_id
                    name = '%s.%s' % (s.get('type'), daemon_id)
                    if s.get('type') in ['rbd-mirror', 'cephfs-mirror', 'rgw', 'rgw-nfs']:
                        metadata = self.mgr.get_metadata(
                            cast(str, s.get('type')), daemon_id, {})
                        assert metadata is not None
                        try:
                            if s.get('type') == 'rgw-nfs':
                                # https://tracker.ceph.com/issues/49573
                                name = metadata['id'][:-4]
                            else:
                                name = '%s.%s' % (s.get('type'), metadata['id'])
                        except (KeyError, TypeError):
                            self.log.debug(
                                "Failed to find daemon id for %s service %s" % (
                                    s.get('type'), s.get('id')
                                )
                            )

                    if host not in self.mgr.inventory:
                        missing_names.append(name)
                        host_num_daemons += 1
                    if name not in managed:
                        daemon_detail.append(
                            'stray daemon %s on host %s not managed by cephadm' % (name, host))
                if missing_names:
                    host_detail.append(
                        'stray host %s has %d stray daemons: %s' % (
                            host, len(missing_names), missing_names))
            if self.mgr.warn_on_stray_hosts and host_detail:
                self.mgr.health_checks['CEPHADM_STRAY_HOST'] = {
                    'severity': 'warning',
                    'summary': '%d stray host(s) with %s daemon(s) '
                    'not managed by cephadm' % (
                        len(host_detail), host_num_daemons),
                    'count': len(host_detail),
                    'detail': host_detail,
                }
            if self.mgr.warn_on_stray_daemons and daemon_detail:
                self.mgr.health_checks['CEPHADM_STRAY_DAEMON'] = {
                    'severity': 'warning',
                    'summary': '%d stray daemon(s) not managed by cephadm' % (
                        len(daemon_detail)),
                    'count': len(daemon_detail),
                    'detail': daemon_detail,
                }
        self.mgr.set_health_checks(self.mgr.health_checks)

    def _apply_all_services(self) -> bool:
        r = False
        specs = []  # type: List[ServiceSpec]
        # if metadata is not up to date, we still need to apply spec for agent
        # since the agent is the one who gather the metadata. If we don't we
        # end up stuck between wanting metadata to be up to date to apply specs
        # and needing to apply the agent spec to get up to date metadata
        if self.mgr.use_agent and not self.mgr.cache.all_host_metadata_up_to_date():
            self.log.info('Metadata not up to date on all hosts. Skipping non agent specs')
            try:
                specs.append(self.mgr.spec_store['agent'].spec)
            except Exception as e:
                self.log.debug(f'Failed to find agent spec: {e}')
                self.mgr.agent_helpers._apply_agent()
                return r
        else:
            for sn, spec in self.mgr.spec_store.active_specs.items():
                specs.append(spec)
        for spec in specs:
            try:
                if self._apply_service(spec):
                    r = True
            except Exception as e:
                self.log.exception('Failed to apply %s spec %s: %s' % (
                    spec.service_name(), spec, e))
                self.mgr.events.for_service(spec, 'ERROR', 'Failed to apply: ' + str(e))

        return r

    def _apply_service_config(self, spec: ServiceSpec) -> None:
        if spec.config:
            section = utils.name_to_config_section(spec.service_name())
            for k, v in spec.config.items():
                try:
                    current = self.mgr.get_foreign_ceph_option(section, k)
                except KeyError:
                    self.log.warning(
                        f'Ignoring invalid {spec.service_name()} config option {k}'
                    )
                    self.mgr.events.for_service(
                        spec, OrchestratorEvent.ERROR, f'Invalid config option {k}'
                    )
                    continue
                if current != v:
                    self.log.debug(f'setting [{section}] {k} = {v}')
                    try:
                        self.mgr.check_mon_command({
                            'prefix': 'config set',
                            'name': k,
                            'value': str(v),
                            'who': section,
                        })
                    except MonCommandFailed as e:
                        self.log.warning(
                            f'Failed to set {spec.service_name()} option {k}: {e}'
                        )

    def _apply_service(self, spec: ServiceSpec) -> bool:
        """
        Schedule a service.  Deploy new daemons or remove old ones, depending
        on the target label and count specified in the placement.
        """
        self.mgr.migration.verify_no_migration()

        service_type = spec.service_type
        service_name = spec.service_name()
        if spec.unmanaged:
            self.log.debug('Skipping unmanaged service %s' % service_name)
            return False
        if spec.preview_only:
            self.log.debug('Skipping preview_only service %s' % service_name)
            return False
        self.log.debug('Applying service %s spec' % service_name)

        if service_type == 'agent':
            try:
                assert self.mgr.cherrypy_thread
                assert self.mgr.cherrypy_thread.ssl_certs.get_root_cert()
            except Exception:
                self.log.info(
                    'Delaying applying agent spec until cephadm endpoint root cert created')
                return False

        self._apply_service_config(spec)

        if service_type == 'osd':
            self.mgr.osd_service.create_from_spec(cast(DriveGroupSpec, spec))
            # TODO: return True would result in a busy loop
            # can't know if daemon count changed; create_from_spec doesn't
            # return a solid indication
            return False

        svc = self.mgr.cephadm_services[service_type]
        daemons = self.mgr.cache.get_daemons_by_service(service_name)

        public_networks: List[str] = []
        if service_type == 'mon':
            out = str(self.mgr.get_foreign_ceph_option('mon', 'public_network'))
            if '/' in out:
                public_networks = [x.strip() for x in out.split(',')]
                self.log.debug('mon public_network(s) is %s' % public_networks)

        def matches_network(host):
            # type: (str) -> bool
            # make sure we have 1 or more IPs for any of those networks on that
            # host
            for network in public_networks:
                if len(self.mgr.cache.networks[host].get(network, [])) > 0:
                    return True
            self.log.info(
                f"Filtered out host {host}: does not belong to mon public_network"
                f" ({','.join(public_networks)})"
            )
            return False

        rank_map = None
        if svc.ranked():
            rank_map = self.mgr.spec_store[spec.service_name()].rank_map or {}
        ha = HostAssignment(
            spec=spec,
            hosts=self.mgr.cache.get_non_draining_hosts() if spec.service_name(
            ) == 'agent' else self.mgr.cache.get_schedulable_hosts(),
            unreachable_hosts=self.mgr.cache.get_unreachable_hosts(),
            daemons=daemons,
            networks=self.mgr.cache.networks,
            filter_new_host=(
                matches_network if service_type == 'mon'
                else None
            ),
            allow_colo=svc.allow_colo(),
            primary_daemon_type=svc.primary_daemon_type(),
            per_host_daemon_type=svc.per_host_daemon_type(),
            rank_map=rank_map,
        )

        try:
            all_slots, slots_to_add, daemons_to_remove = ha.place()
            daemons_to_remove = [d for d in daemons_to_remove if (d.hostname and self.mgr.inventory._inventory[d.hostname].get(
                'status', '').lower() not in ['maintenance', 'offline'] and d.hostname not in self.mgr.offline_hosts)]
            self.log.debug('Add %s, remove %s' % (slots_to_add, daemons_to_remove))
        except OrchestratorError as e:
            self.log.error('Failed to apply %s spec %s: %s' % (
                spec.service_name(), spec, e))
            self.mgr.events.for_service(spec, 'ERROR', 'Failed to apply: ' + str(e))
            return False

        r = None

        # sanity check
        final_count = len(daemons) + len(slots_to_add) - len(daemons_to_remove)
        if service_type in ['mon', 'mgr'] and final_count < 1:
            self.log.debug('cannot scale mon|mgr below 1)')
            return False

        # progress
        progress_id = str(uuid.uuid4())
        delta: List[str] = []
        if slots_to_add:
            delta += [f'+{len(slots_to_add)}']
        if daemons_to_remove:
            delta += [f'-{len(daemons_to_remove)}']
        progress_title = f'Updating {spec.service_name()} deployment ({" ".join(delta)} -> {len(all_slots)})'
        progress_total = len(slots_to_add) + len(daemons_to_remove)
        progress_done = 0

        def update_progress() -> None:
            self.mgr.remote(
                'progress', 'update', progress_id,
                ev_msg=progress_title,
                ev_progress=(progress_done / progress_total),
                add_to_ceph_s=True,
            )

        if progress_total:
            update_progress()

        # add any?
        did_config = False

        self.log.debug('Hosts that will receive new daemons: %s' % slots_to_add)
        self.log.debug('Daemons that will be removed: %s' % daemons_to_remove)

        hosts_altered: Set[str] = set()

        try:
            # assign names
            for i in range(len(slots_to_add)):
                slot = slots_to_add[i]
                slot = slot.assign_name(self.mgr.get_unique_name(
                    slot.daemon_type,
                    slot.hostname,
                    daemons,
                    prefix=spec.service_id,
                    forcename=slot.name,
                    rank=slot.rank,
                    rank_generation=slot.rank_generation,
                ))
                slots_to_add[i] = slot
                if rank_map is not None:
                    assert slot.rank is not None
                    assert slot.rank_generation is not None
                    assert rank_map[slot.rank][slot.rank_generation] is None
                    rank_map[slot.rank][slot.rank_generation] = slot.name

            if rank_map:
                # record the rank_map before we make changes so that if we fail the
                # next mgr will clean up.
                self.mgr.spec_store.save_rank_map(spec.service_name(), rank_map)

                # remove daemons now, since we are going to fence them anyway
                for d in daemons_to_remove:
                    assert d.hostname is not None
                    self._remove_daemon(d.name(), d.hostname)
                daemons_to_remove = []

                # fence them
                svc.fence_old_ranks(spec, rank_map, len(all_slots))

            # create daemons
            for slot in slots_to_add:
                # first remove daemon on conflicting port?
                if slot.ports:
                    for d in daemons_to_remove:
                        if d.hostname != slot.hostname:
                            continue
                        if not (set(d.ports or []) & set(slot.ports)):
                            continue
                        if d.ip and slot.ip and d.ip != slot.ip:
                            continue
                        self.log.info(
                            f'Removing {d.name()} before deploying to {slot} to avoid a port conflict'
                        )
                        # NOTE: we don't check ok-to-stop here to avoid starvation if
                        # there is only 1 gateway.
                        self._remove_daemon(d.name(), d.hostname)
                        daemons_to_remove.remove(d)
                        progress_done += 1
                        hosts_altered.add(d.hostname)
                        break

                # deploy new daemon
                daemon_id = slot.name
                if not did_config:
                    svc.config(spec)
                    did_config = True

                daemon_spec = svc.make_daemon_spec(
                    slot.hostname, daemon_id, slot.network, spec,
                    daemon_type=slot.daemon_type,
                    ports=slot.ports,
                    ip=slot.ip,
                    rank=slot.rank,
                    rank_generation=slot.rank_generation,
                )
                self.log.debug('Placing %s.%s on host %s' % (
                    slot.daemon_type, daemon_id, slot.hostname))

                try:
                    daemon_spec = svc.prepare_create(daemon_spec)
                    self._create_daemon(daemon_spec)
                    r = True
                    progress_done += 1
                    update_progress()
                    hosts_altered.add(daemon_spec.host)
                except (RuntimeError, OrchestratorError) as e:
                    msg = (f"Failed while placing {slot.daemon_type}.{daemon_id} "
                           f"on {slot.hostname}: {e}")
                    self.mgr.events.for_service(spec, 'ERROR', msg)
                    self.mgr.log.error(msg)
                    # only return "no change" if no one else has already succeeded.
                    # later successes will also change to True
                    if r is None:
                        r = False
                    progress_done += 1
                    update_progress()
                    continue

                # add to daemon list so next name(s) will also be unique
                sd = orchestrator.DaemonDescription(
                    hostname=slot.hostname,
                    daemon_type=slot.daemon_type,
                    daemon_id=daemon_id,
                )
                daemons.append(sd)

            # remove any?
            def _ok_to_stop(remove_daemons: List[orchestrator.DaemonDescription]) -> bool:
                daemon_ids = [d.daemon_id for d in remove_daemons]
                assert None not in daemon_ids
                # setting force flag retains previous behavior
                r = svc.ok_to_stop(cast(List[str], daemon_ids), force=True)
                return not r.retval

            while daemons_to_remove and not _ok_to_stop(daemons_to_remove):
                # let's find a subset that is ok-to-stop
                daemons_to_remove.pop()
            for d in daemons_to_remove:
                r = True
                assert d.hostname is not None
                self._remove_daemon(d.name(), d.hostname)

                progress_done += 1
                update_progress()
                hosts_altered.add(d.hostname)

            self.mgr.remote('progress', 'complete', progress_id)
        except Exception as e:
            self.mgr.remote('progress', 'fail', progress_id, str(e))
            raise
        finally:
            if self.mgr.use_agent:
                # can only send ack to agents if we know for sure port they bound to
                hosts_altered = set([h for h in hosts_altered if (h in self.mgr.cache.agent_ports and h in [
                                    h2.hostname for h2 in self.mgr.cache.get_non_draining_hosts()])])
                self.mgr.agent_helpers._request_agent_acks(hosts_altered, increment=True)

        if r is None:
            r = False
        return r

    def _check_daemons(self) -> None:

        daemons = self.mgr.cache.get_daemons()
        daemons_post: Dict[str, List[orchestrator.DaemonDescription]] = defaultdict(list)
        for dd in daemons:
            # orphan?
            spec = self.mgr.spec_store.active_specs.get(dd.service_name(), None)
            assert dd.hostname is not None
            assert dd.daemon_type is not None
            assert dd.daemon_id is not None
            if not spec and dd.daemon_type not in ['mon', 'mgr', 'osd']:
                # (mon and mgr specs should always exist; osds aren't matched
                # to a service spec)
                self.log.info('Removing orphan daemon %s...' % dd.name())
                self._remove_daemon(dd.name(), dd.hostname)

            # ignore unmanaged services
            if spec and spec.unmanaged:
                continue

            # ignore daemons for deleted services
            if dd.service_name() in self.mgr.spec_store.spec_deleted:
                continue

            if dd.daemon_type == 'agent':
                try:
                    assert self.mgr.cherrypy_thread
                    assert self.mgr.cherrypy_thread.ssl_certs.get_root_cert()
                except Exception:
                    self.log.info(
                        f'Delaying checking {dd.name()} until cephadm endpoint finished creating root cert')
                    continue

            # These daemon types require additional configs after creation
            if dd.daemon_type in REQUIRES_POST_ACTIONS:
                daemons_post[dd.daemon_type].append(dd)

            if self.mgr.cephadm_services[daemon_type_to_service(dd.daemon_type)].get_active_daemon(
               self.mgr.cache.get_daemons_by_service(dd.service_name())).daemon_id == dd.daemon_id:
                dd.is_active = True
            else:
                dd.is_active = False

            deps = self.mgr._calc_daemon_deps(spec, dd.daemon_type, dd.daemon_id)
            last_deps, last_config = self.mgr.cache.get_daemon_last_config_deps(
                dd.hostname, dd.name())
            if last_deps is None:
                last_deps = []
            action = self.mgr.cache.get_scheduled_daemon_action(dd.hostname, dd.name())
            if not last_config:
                self.log.info('Reconfiguring %s (unknown last config time)...' % (
                    dd.name()))
                action = 'reconfig'
            elif last_deps != deps:
                self.log.debug('%s deps %s -> %s' % (dd.name(), last_deps,
                                                     deps))
                self.log.info('Reconfiguring %s (dependencies changed)...' % (
                    dd.name()))
                action = 'reconfig'
            elif self.mgr.last_monmap and \
                    self.mgr.last_monmap > last_config and \
                    dd.daemon_type in CEPH_TYPES:
                self.log.info('Reconfiguring %s (monmap changed)...' % dd.name())
                action = 'reconfig'
            elif self.mgr.extra_ceph_conf_is_newer(last_config) and \
                    dd.daemon_type in CEPH_TYPES:
                self.log.info('Reconfiguring %s (extra config changed)...' % dd.name())
                action = 'reconfig'
            if action:
                if self.mgr.cache.get_scheduled_daemon_action(dd.hostname, dd.name()) == 'redeploy' \
                        and action == 'reconfig':
                    action = 'redeploy'
                try:
                    daemon_spec = CephadmDaemonDeploySpec.from_daemon_description(dd)
                    self.mgr._daemon_action(daemon_spec, action=action)
                    self.mgr.cache.rm_scheduled_daemon_action(dd.hostname, dd.name())
                except OrchestratorError as e:
                    self.mgr.events.from_orch_error(e)
                    if dd.daemon_type in daemons_post:
                        del daemons_post[dd.daemon_type]
                    # continue...
                except Exception as e:
                    self.mgr.events.for_daemon_from_exception(dd.name(), e)
                    if dd.daemon_type in daemons_post:
                        del daemons_post[dd.daemon_type]
                    # continue...

        # do daemon post actions
        for daemon_type, daemon_descs in daemons_post.items():
            if daemon_type in self.mgr.requires_post_actions:
                self.mgr.requires_post_actions.remove(daemon_type)
                self.mgr._get_cephadm_service(daemon_type_to_service(
                    daemon_type)).daemon_check_post(daemon_descs)

    def _purge_deleted_services(self) -> None:
        existing_services = self.mgr.spec_store.all_specs.items()
        for service_name, spec in list(existing_services):
            if service_name not in self.mgr.spec_store.spec_deleted:
                continue
            if self.mgr.cache.get_daemons_by_service(service_name):
                continue
            if spec.service_type in ['mon', 'mgr']:
                continue

            logger.info(f'Purge service {service_name}')

            self.mgr.cephadm_services[spec.service_type].purge(service_name)
            self.mgr.spec_store.finally_rm(service_name)

    def convert_tags_to_repo_digest(self) -> None:
        if not self.mgr.use_repo_digest:
            return
        settings = self.mgr.upgrade.get_distinct_container_image_settings()
        digests: Dict[str, ContainerInspectInfo] = {}
        for container_image_ref in set(settings.values()):
            if not is_repo_digest(container_image_ref):
                image_info = self._get_container_image_info(container_image_ref)
                if image_info.repo_digests:
                    # FIXME: we assume the first digest here is the best
                    assert is_repo_digest(image_info.repo_digests[0]), image_info
                digests[container_image_ref] = image_info

        for entity, container_image_ref in settings.items():
            if not is_repo_digest(container_image_ref):
                image_info = digests[container_image_ref]
                if image_info.repo_digests:
                    # FIXME: we assume the first digest here is the best
                    self.mgr.set_container_image(entity, image_info.repo_digests[0])

    def _create_daemon(self,
                       daemon_spec: CephadmDaemonDeploySpec,
                       reconfig: bool = False,
                       osd_uuid_map: Optional[Dict[str, Any]] = None,
                       ) -> str:

        with set_exception_subject('service', orchestrator.DaemonDescription(
                daemon_type=daemon_spec.daemon_type,
                daemon_id=daemon_spec.daemon_id,
                hostname=daemon_spec.host,
        ).service_id(), overwrite=True):

            try:
                image = ''
                start_time = datetime_now()
                ports: List[int] = daemon_spec.ports if daemon_spec.ports else []

                if daemon_spec.daemon_type == 'container':
                    spec = cast(CustomContainerSpec,
                                self.mgr.spec_store[daemon_spec.service_name].spec)
                    image = spec.image
                    if spec.ports:
                        ports.extend(spec.ports)

                # TCP port to open in the host firewall
                if len(ports) > 0:
                    daemon_spec.extra_args.extend([
                        '--tcp-ports', ' '.join(map(str, ports))
                    ])

                # osd deployments needs an --osd-uuid arg
                if daemon_spec.daemon_type == 'osd':
                    if not osd_uuid_map:
                        osd_uuid_map = self.mgr.get_osd_uuid_map()
                    osd_uuid = osd_uuid_map.get(daemon_spec.daemon_id)
                    if not osd_uuid:
                        raise OrchestratorError('osd.%s not in osdmap' % daemon_spec.daemon_id)
                    daemon_spec.extra_args.extend(['--osd-fsid', osd_uuid])

                if reconfig:
                    daemon_spec.extra_args.append('--reconfig')
                if self.mgr.allow_ptrace:
                    daemon_spec.extra_args.append('--allow-ptrace')

                if self.mgr.cache.host_needs_registry_login(daemon_spec.host) and self.mgr.registry_url:
                    self._registry_login(daemon_spec.host, self.mgr.registry_url,
                                         self.mgr.registry_username, self.mgr.registry_password)

                self.log.info('%s daemon %s on %s' % (
                    'Reconfiguring' if reconfig else 'Deploying',
                    daemon_spec.name(), daemon_spec.host))

                out, err, code = self._run_cephadm(
                    daemon_spec.host, daemon_spec.name(), 'deploy',
                    [
                        '--name', daemon_spec.name(),
                        '--meta-json', json.dumps({
                            'service_name': daemon_spec.service_name,
                            'ports': daemon_spec.ports,
                            'ip': daemon_spec.ip,
                            'deployed_by': self.mgr.get_active_mgr_digests(),
                            'rank': daemon_spec.rank,
                            'rank_generation': daemon_spec.rank_generation,
                        }),
                        '--config-json', '-',
                    ] + daemon_spec.extra_args,
                    stdin=json.dumps(daemon_spec.final_config),
                    image=image)

                if daemon_spec.daemon_type == 'agent':
                    self.mgr.cache.agent_timestamp[daemon_spec.host] = datetime_now()

                # refresh daemon state?  (ceph daemon reconfig does not need it)
                if not reconfig or daemon_spec.daemon_type not in CEPH_TYPES:
                    if not code and daemon_spec.host in self.mgr.cache.daemons:
                        # prime cached service state with what we (should have)
                        # just created
                        sd = daemon_spec.to_daemon_description(
                            DaemonDescriptionStatus.running, 'starting')
                        self.mgr.cache.add_daemon(daemon_spec.host, sd)
                        if daemon_spec.daemon_type in REQUIRES_POST_ACTIONS:
                            self.mgr.requires_post_actions.add(daemon_spec.daemon_type)
                    self.mgr.cache.invalidate_host_daemons(daemon_spec.host)

                self.mgr.cache.update_daemon_config_deps(
                    daemon_spec.host, daemon_spec.name(), daemon_spec.deps, start_time)
                self.mgr.cache.save_host(daemon_spec.host)
                msg = "{} {} on host '{}'".format(
                    'Reconfigured' if reconfig else 'Deployed', daemon_spec.name(), daemon_spec.host)
                if not code:
                    self.mgr.events.for_daemon(daemon_spec.name(), OrchestratorEvent.INFO, msg)
                else:
                    what = 'reconfigure' if reconfig else 'deploy'
                    self.mgr.events.for_daemon(
                        daemon_spec.name(), OrchestratorEvent.ERROR, f'Failed to {what}: {err}')
                return msg
            except OrchestratorError:
                redeploy = daemon_spec.name() in self.mgr.cache.get_daemon_names()
                if not reconfig and not redeploy:
                    # we have to clean up the daemon. E.g. keyrings.
                    servict_type = daemon_type_to_service(daemon_spec.daemon_type)
                    dd = daemon_spec.to_daemon_description(DaemonDescriptionStatus.error, 'failed')
                    self.mgr.cephadm_services[servict_type].post_remove(dd, is_failed_deploy=True)
                raise

    def _remove_daemon(self, name: str, host: str) -> str:
        """
        Remove a daemon
        """
        (daemon_type, daemon_id) = name.split('.', 1)
        daemon = orchestrator.DaemonDescription(
            daemon_type=daemon_type,
            daemon_id=daemon_id,
            hostname=host)

        with set_exception_subject('service', daemon.service_id(), overwrite=True):

            self.mgr.cephadm_services[daemon_type_to_service(daemon_type)].pre_remove(daemon)

            # NOTE: we are passing the 'force' flag here, which means
            # we can delete a mon instances data.
            args = ['--name', name, '--force']
            self.log.info('Removing daemon %s from %s' % (name, host))
            out, err, code = self._run_cephadm(
                host, name, 'rm-daemon', args)
            if not code:
                # remove item from cache
                self.mgr.cache.rm_daemon(host, name)
            self.mgr.cache.invalidate_host_daemons(host)

            self.mgr.cephadm_services[daemon_type_to_service(
                daemon_type)].post_remove(daemon, is_failed_deploy=False)

            return "Removed {} from host '{}'".format(name, host)

    def _run_cephadm_json(self,
                          host: str,
                          entity: Union[CephadmNoImage, str],
                          command: str,
                          args: List[str],
                          no_fsid: Optional[bool] = False,
                          image: Optional[str] = "",
                          ) -> Any:
        try:
            out, err, code = self._run_cephadm(
                host, entity, command, args, no_fsid=no_fsid, image=image)
            if code:
                raise OrchestratorError(f'host {host} `cephadm {command}` returned {code}: {err}')
        except Exception as e:
            raise OrchestratorError(f'host {host} `cephadm {command}` failed: {e}')
        try:
            return json.loads(''.join(out))
        except (ValueError, KeyError):
            msg = f'host {host} `cephadm {command}` failed: Cannot decode JSON'
            self.log.exception(f'{msg}: {"".join(out)}')
            raise OrchestratorError(msg)

    def _run_cephadm(self,
                     host: str,
                     entity: Union[CephadmNoImage, str],
                     command: str,
                     args: List[str],
                     addr: Optional[str] = "",
                     stdin: Optional[str] = "",
                     no_fsid: Optional[bool] = False,
                     error_ok: Optional[bool] = False,
                     image: Optional[str] = "",
                     env_vars: Optional[List[str]] = None,
                     ) -> Tuple[List[str], List[str], int]:
        """
        Run cephadm on the remote host with the given command + args

        Important: You probably don't want to run _run_cephadm from CLI handlers

        :env_vars: in format -> [KEY=VALUE, ..]
        """

        self.mgr.ssh.remote_connection(host, addr)

        self.log.debug(f"_run_cephadm : command = {command}")
        self.log.debug(f"_run_cephadm : args = {args}")

        bypass_image = ('agent')

        assert image or entity
        # Skip the image check for daemons deployed that are not ceph containers
        if not str(entity).startswith(bypass_image):
            if not image and entity is not cephadmNoImage:
                image = self.mgr._get_container_image(entity)

        final_args = []

        # global args
        if env_vars:
            for env_var_pair in env_vars:
                final_args.extend(['--env', env_var_pair])

        if image:
            final_args.extend(['--image', image])

        if not self.mgr.container_init:
            final_args += ['--no-container-init']

        # subcommand
        final_args.append(command)

        # subcommand args
        if not no_fsid:
            final_args += ['--fsid', self.mgr._cluster_fsid]

        final_args += args

        # exec
        self.log.debug('args: %s' % (' '.join(final_args)))
        if self.mgr.mode == 'root':
            # agent has cephadm binary as an extra file which is
            # therefore passed over stdin. Even for debug logs it's too much
            if stdin and 'agent' not in str(entity):
                self.log.debug('stdin: %s' % stdin)

            cmd = ['which', 'python3']
            python = self.mgr.ssh.check_execute_command(host, cmd, addr=addr)
            cmd = [python, self.mgr.cephadm_binary_path] + final_args

            try:
                out, err, code = self.mgr.ssh.execute_command(
                    host, cmd, stdin=stdin.encode('utf-8') if stdin else None, addr=addr)
                if code == 2:
                    ls_cmd = ['ls', self.mgr.cephadm_binary_path]
                    out_ls, err_ls, code_ls = self.mgr.ssh.execute_command(host, ls_cmd, addr=addr)
                    if code_ls == 2:
                        self._deploy_cephadm_binary(host, addr)
                        out, err, code = self.mgr.ssh.execute_command(
                            host, cmd, stdin=stdin.encode('utf-8') if stdin else None, addr=addr)

            except Exception as e:
                self.mgr.ssh._reset_con(host)
                if error_ok:
                    return [], [str(e)], 1
                raise

        elif self.mgr.mode == 'cephadm-package':
            try:
                cmd = ['/usr/bin/cephadm'] + final_args
                out, err, code = self.mgr.ssh.execute_command(
                    host, cmd, stdin=stdin.encode('utf-8') if stdin else None, addr=addr)
            except Exception as e:
                self.mgr.ssh._reset_con(host)
                if error_ok:
                    return [], [str(e)], 1
                raise
        else:
            assert False, 'unsupported mode'

        self.log.debug(f'code: {code}')
        if out:
            self.log.debug(f'out: {out}')
        if err:
            self.log.debug(f'err: {err}')
        if code and not error_ok:
            raise OrchestratorError(
                f'cephadm exited with an error code: {code}, stderr: {err}')
        return [out], [err], code

    def _get_container_image_info(self, image_name: str) -> ContainerInspectInfo:
        # pick a random host...
        host = None
        for host_name in self.mgr.inventory.keys():
            host = host_name
            break
        if not host:
            raise OrchestratorError('no hosts defined')
        if self.mgr.cache.host_needs_registry_login(host) and self.mgr.registry_url:
            self._registry_login(host, self.mgr.registry_url,
                                 self.mgr.registry_username, self.mgr.registry_password)

        j = self._run_cephadm_json(host, '', 'pull', [], image=image_name, no_fsid=True)

        r = ContainerInspectInfo(
            j['image_id'],
            j.get('ceph_version'),
            j.get('repo_digests')
        )
        self.log.debug(f'image {image_name} -> {r}')
        return r

    # function responsible for logging single host into custom registry
    def _registry_login(self, host: str, url: Optional[str], username: Optional[str], password: Optional[str]) -> Optional[str]:
        self.log.debug(f"Attempting to log host {host} into custom registry @ {url}")
        # want to pass info over stdin rather than through normal list of args
        args_str = json.dumps({
            'url': url,
            'username': username,
            'password': password,
        })
        out, err, code = self._run_cephadm(
            host, 'mon', 'registry-login',
            ['--registry-json', '-'], stdin=args_str, error_ok=True)
        if code:
            return f"Host {host} failed to login to {url} as {username} with given password"
        return None

    def _deploy_cephadm_binary(self, host: str, addr: Optional[str] = None) -> None:
        # Use tee (from coreutils) to create a copy of cephadm on the target machine
        self.log.info(f"Deploying cephadm binary to {host}")
        self.mgr.ssh.write_remote_file(host, self.mgr.cephadm_binary_path,
                                       self.mgr._cephadm.encode('utf-8'), addr=addr)
