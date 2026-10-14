#!/opt/coralnpu-ci/venv/bin/python -I
"""Fixed root entrypoint. No config/path/command overrides from CI."""
import json
import os
from pathlib import Path
import signal
import sys
import time

# -I rejects PYTHONPATH/user site. Only root-owned installed sibling code is used.
INSTALL = Path('/usr/local/libexec/coralnpu-ci')
if __name__ == '__main__':
    if os.geteuid() != 0 or Path(__file__).resolve().parent != INSTALL:
        raise SystemExit('fixed installed root launcher required')
    for path in [INSTALL, *INSTALL.parents]:
        info = path.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or path.is_symlink():
            raise SystemExit('untrusted launcher installation')
    sys.path.insert(0, str(INSTALL))


def main(argv=None):
    from container_argv import run_key
    from secure_files import open_trusted_directory, read_private_json
    from store import RootStore
    from linux_adapter import LinuxAdapter
    from runtime import Runtime
    from deadline_guard import Guard
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in {'submit', 'launch', 'cleanup', 'watch'}:
        raise ValueError('fixed phase required')
    operation = args[0]
    if len(args) != (1 if operation == 'submit' else 2):
        raise ValueError('unexpected arguments')
    fd = open_trusted_directory('/etc/coralnpu-ci', private=True)
    try:
        config = read_private_json(fd, 'config.json')
    finally:
        os.close(fd)
    store = RootStore('/var/lib/coralnpu-ci', config['pilot_id'])
    adapter = LinuxAdapter(config)

    def publish(cfg, request, inbox, trusted):
        # Import/authentication happens after all guest cgroups stopped and inbox sealed.
        from transfer.host_upload import publish as upload, create_host_s3
        return upload(
            cfg,
            request,
            inbox,
            trusted,
            create_host_s3(cfg),
            expected_owner=0
        )

    runtime = Runtime(config, store, adapter, publish)
    if operation == 'submit':
        result = runtime.submit(os.environ)
        key = result['run_key']
        if os.environ.get('SSM_Operation', 'Build') == 'Cancel':
            print(json.dumps(result))
            return

        def interrupted(signum, frame):
            runtime.cancel(key, store.read_request(key))
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        # SSM does not report success until the durable terminal result exists.
        end = time.monotonic() + 21900
        while time.monotonic() < end:
            state = store.read_status(key)
            if state in {'success', 'failed', 'timed_out', 'cancelled',
                         'quarantined'}:
                print(json.dumps({'run_key': key, 'state': state}))
                raise SystemExit(0 if state == 'success' else 1)
            time.sleep(1)
        runtime.cancel(key, store.read_request(key))
        raise TimeoutError('SSM wait expired; exact request cancelled')
    key = run_key(args[1])
    if operation == 'launch':
        print(json.dumps(runtime.launch(key)))
    elif operation == 'cleanup':
        runtime.cleanup(key)
    else:
        Guard(store).run(key)


if __name__ == '__main__':
    main()
