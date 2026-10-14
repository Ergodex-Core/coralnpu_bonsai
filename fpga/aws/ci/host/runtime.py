"""Credential-free supervisor logic; Linux side effects are injected explicitly.

Only root-installed code imports this module. The source workspace is data, never
an import search path or host command. Unresolved config cannot call the adapter.
"""
import time
from container_argv import run_key
from protocol import decode_ssm_environment, validate_request
from validate_bundle import activation_errors


class Cancelled(RuntimeError):
    pass


class Blocked(RuntimeError):
    pass


class Runtime:

    def __init__(self, config, store, adapter, publish, *, now=time.time):
        self.config, self.store, self.adapter, self.publish = config, store, adapter, publish
        self.now = now

    def enabled(self):
        errors = activation_errors(self.config)
        required = (
            'host_runtime_verified', 'aggregate_cgroup_limits_verified',
            'immutable_entrypoints_verified', 'staging_network_verified'
        )
        errors += [
            key + ' unqualified'
            for key in required
            if self.config.get(key) is not True
        ]
        if self.config.get('repository') != 'Ergodex-Core/coralnpu_bonsai':
            errors.append('repository differs from installed trust boundary')
        for key, value in {'maximum_distinct_admissions': 3,
                           'maximum_parallel_jobs': 1, 'runtime_seconds':
                           21600, 'container_cpus': 16,
                           'container_memory_bytes': 68719476736,
                           'container_pids': 4096, 'per_run_disk_bytes':
                           268435456000, 'per_run_inode_limit':
                           2000000}.items():
            if type(self.config.get(key)
                    ) is not int or self.config[key] != value:
                errors.append('reviewed bound mismatch: ' + key)
        if errors:
            raise Blocked('; '.join(errors))

    def submit(self, environment):
        request = decode_ssm_environment(environment, self.config)
        key = run_key(request['run_id'] + '-' + request['attempt'])
        if environment['SSM_Operation'] == 'Cancel':
            return self.cancel(key, request)
        self.enabled()
        start_error = None
        with self.store.request_lock(key):
            created = self.store.create_request(key, request)
            cancelled = self.store.cancel_requested(key)
            if created and not cancelled:
                try:
                    self.adapter.start_launcher(key)
                except Exception as exc:
                    start_error = exc
        if start_error is not None:
            self.store.finish(request, 'failed', error='launcher start failed')
            raise start_error
        return {'run_key': key, 'state': self.store.read_status(key)}

    def cancel(self, key, request):
        # Valid even after new admissions are disabled; identity never comes from a unit argument.
        key = run_key(key)
        validate_request(request, self.config)
        if key != request['run_id'] + '-' + request['attempt']:
            raise Blocked('cancellation identity mismatch')
        with self.store.request_lock(key):
            self.store.request_cancel(key, request)
            state = self.store.read_status(key)
            try:
                boundary = self.store.read_boundary(key)
            except FileNotFoundError:
                boundary = None
        if state in {'success', 'failed', 'timed_out', 'cancelled',
                     'quarantined'}:
            return {'run_key': key, 'state': state}
        if boundary is not None:
            try:
                self.adapter.cancel_run(key, boundary)
                self.adapter.assert_empty(boundary)
            except Exception as exc:
                self.store.quarantine(key, str(exc))
                raise
        self.store.finish(
            request, 'cancelled', error='exact request cancellation'
        )
        return {'run_key': key, 'state': self.store.read_status(key)}

    def check_cancel(self, key):
        if self.store.cancel_requested(key):
            raise Cancelled('exact request was cancelled')

    def launch(self, key):
        key = run_key(key)
        request = self.store.read_request(key)
        validate_request(request, self.config)
        if key != request['run_id'] + '-' + request['attempt']:
            raise Blocked('request filename and identity differ')
        lock = None
        admitted = False
        boundary = None
        try:
            self.enabled()
            with self.store.request_lock(key):
                if self.store.read_status(key) in {'success', 'failed',
                                                   'timed_out', 'cancelled',
                                                   'quarantined'}:
                    return {
                        'run_key': key,
                        'state': self.store.read_status(key),
                        'launched': False
                    }
            self.check_cancel(key)
            lock = self.adapter.acquire_shared_lock()
            self.adapter.validate_permit(request, self.now())
            self.adapter.assert_no_unmanaged_builds()
            self.adapter.validate_host_prerequisites()
            # Hard filesystem measurements precede admission and guest activity.
            self.adapter.ensure_bounded_filesystem()
            with self.store.request_lock(key):
                self.check_cancel(key)
                if self.store.read_status(key) in {'success', 'failed',
                                                   'timed_out', 'cancelled',
                                                   'quarantined'}:
                    return {
                        'run_key': key,
                        'state': self.store.read_status(key),
                        'launched': False
                    }
                self.adapter.mark_admission()
                admission = self.store.admit(request)
                if not admission['launch']:
                    return {
                        'run_key': key,
                        'state': admission['run']['status'],
                        'launched': False
                    }
                admitted = True
                deadline = admission['run']['deadline']
                self.store.set_status(key, 'running')
                paths = self.adapter.prepare_workspace(key)
                boundary = self.adapter.create_boundary(key, deadline)
                self.store.record_boundary(key, boundary)
            # Must return only after independent watcher is active and identity checked.
            self.adapter.arm_deadline(key, boundary, deadline)
            outcome, qualified = 'success', False
            failure = None
            try:
                self.check_cancel(key)
                self.adapter.assert_deadline(deadline)
                self.adapter.assert_empty(boundary)
                stage_code = self.adapter.run_phase(
                    'stage', request, paths, boundary, deadline
                )
                self.adapter.assert_empty(boundary)
                self.check_cancel(key)
                build_code = -1
                if stage_code == 0:
                    build_code = self.adapter.run_phase(
                        'build', request, paths, boundary, deadline
                    )
                    self.adapter.assert_empty(boundary)
                    self.check_cancel(key)
                prior_ok = stage_code == 0 and build_code == 0
                qualify_code = self.adapter.run_phase(
                    'qualify',
                    request,
                    paths,
                    boundary,
                    deadline,
                    qualified=prior_ok
                )
                self.adapter.assert_empty(boundary)
                self.check_cancel(key)
                if not prior_ok or qualify_code != 0:
                    outcome = 'failed'
                    failure = f'stage={stage_code}, build={build_code}, qualification={qualify_code}'
                else:
                    qualified = True
            except (TimeoutError, Cancelled):
                raise
            except Exception:
                # Infrastructure exceptions are uncertain; no further guest phases.
                raise
            self.check_cancel(key)
            self.adapter.assert_empty(boundary)
            if self.adapter.run_phase('collect', request, paths, boundary,
                                      deadline, qualified=qualified) != 0:
                raise RuntimeError('fixed collector failed')
            self.adapter.assert_empty(boundary)
            # Freeze and destroy every named guest before sealing or acquiring credentials.
            self.adapter.retire_guests(key, boundary, deadline)
            self.adapter.assert_empty(boundary)
            self.check_cancel(key)
            inbox = self.adapter.seal_inbox(paths, qualified=qualified)
            try:
                trusted = {
                    'qualified': qualified,
                    'source_sha': request['source_sha'],
                    'source_ref': request['eligibility']['source_ref'],
                    'status': outcome
                }
                receipt = self.publish(self.config, request, inbox, trusted)
            finally:
                self.adapter.close_inbox(inbox)
            self.adapter.assert_deadline(deadline)
            actual = self.store.finish(
                request, outcome, receipt=receipt, error=failure
            )
            self.adapter.disarm_deadline(key)
            return {
                'run_key': key,
                'state': actual,
                'qualified': qualified and actual == 'success'
            }
        except Exception as exc:
            # No guest operation is attempted after an uncertain stop; cleanup errors quarantine state.
            cleanup_ok = boundary is None
            if boundary is not None:
                try:
                    self.adapter.emergency_stop(key, boundary)
                    self.adapter.assert_empty(boundary)
                    cleanup_ok = True
                except Exception as stop_error:
                    self.store.quarantine(key, str(stop_error))
            if cleanup_ok:
                self.store.finish(
                    request,
                    'cancelled' if isinstance(exc, Cancelled) else
                    'timed_out' if isinstance(exc, TimeoutError) else 'failed',
                    error=str(exc)
                )
                if boundary is not None:
                    self.adapter.disarm_deadline(key)
            raise
        finally:
            if lock is not None:
                self.adapter.release_shared_lock(lock)

    def cleanup(self, key):
        # Cleanup intentionally remains usable after an operator disables future admission.
        key = run_key(key)
        request = self.store.read_request(key)
        validate_request(request, self.config)
        try:
            boundary = self.store.read_boundary(key)
        except FileNotFoundError:
            return
        self.adapter.emergency_stop(key, boundary)
        self.adapter.assert_empty(boundary)
        if self.store.read_status(key) == 'running':
            self.store.finish(
                request,
                'cancelled' if self.store.cancel_requested(key) else 'failed',
                error='service cleanup'
            )
