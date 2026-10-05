import base64
import copy
from contextlib import redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'orchestrator'), str(ROOT / 'host')]
from protocol import make_request, encode_request, decode_ssm_environment, validate_request
from transfer.host_upload import publish, create_host_s3
from controller import submit_and_collect, oidc_token
from configure import configure
from validate_bundle import activation_errors

SHA = 'a' * 40


def config_fixture():
    config = json.loads((ROOT / 'config.example.json').read_text())
    for key, value in list(config.items()):
        if value is False:
            config[key] = True
        if isinstance(value, str) and 'UNRESOLVED' in value:
            config[key] = 'test-only-qualified-value'
    config.update(
        aws_account='111122223333',
        document_numeric_version='1',
        document_aws_sha256='d' * 64,
        verified_oidc_subject='repo:example/project:ref:refs/heads/main',
        expected_oidc_subject_unverified=
        'repo:example/project:ref:refs/heads/main',
        host_uid=2100,
        host_gid=2100,
        container_image_digest='registry.example/ci@sha256:' + 'e' * 64
    )
    assert not activation_errors(config), activation_errors(config)
    return config


def proof_fixture(config):
    return {
        'repository': config['repository'],
        'repository_id': config['repository_id'],
        'pull_request': 11,
        'source_sha': SHA,
        'source_ref': 'refs/pull/11/head',
        'source_kind': 'pr_head_exact_sha',
        'source_run_id': 20,
        'source_run_head_sha': SHA,
        'source_job_id': 21,
        'eligibility_reason': 'current-maintainer-review-of-exact-head-sha',
        'exact_sha_approvers': ['maintainer']
    }


class FakeS3:

    def __init__(self):
        self.objects = {}
        self.calls = []

    def put_object(self, **kwargs):
        self.calls.append(('put', kwargs['Key']))
        if kwargs['Key'] in self.objects:
            raise RuntimeError('precondition failed')
        assert kwargs['IfNoneMatch'] == '*'
        assert kwargs['ServerSideEncryption'] == 'AES256'
        assert kwargs['ExpectedBucketOwner'] == '111122223333'
        data = kwargs['Body'].read()
        assert len(data) == kwargs['ContentLength']
        assert base64.b64encode(hashlib.sha256(data).digest()
                                ).decode() == kwargs['ChecksumSHA256']
        self.objects[kwargs['Key']] = data
        return {
            'ResponseMetadata': {
                'HTTPStatusCode': 200
            },
            'ChecksumSHA256': kwargs['ChecksumSHA256']
        }

    def get_object(self, **kwargs):
        self.calls.append(('get', kwargs['Key']))
        data = self.objects[kwargs['Key']]
        return {'Body': io.BytesIO(data), 'ContentLength': len(data)}


class TransportTests(unittest.TestCase):

    def setUp(self):
        self.config = config_fixture()
        self.proof = proof_fixture(self.config)
        self.request = make_request(self.proof, self.config, '123', '1')
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.inbox = self.directory / 'inbox'
        self.inbox.mkdir(mode=0o700)
        for name in ('checkpoint.tar', 'evidence.tar'):
            path = self.inbox / name
            path.write_bytes((name + '-opaque-data').encode())
            path.chmod(0o400)
        self.fd = os.open(self.inbox, os.O_RDONLY)
        self.s3 = FakeS3()
        self.result = {
            'qualified': True,
            'source_sha': SHA,
            'source_ref': 'refs/pull/11/head',
            'status': 'success'
        }

    def tearDown(self):
        os.close(self.fd)
        self.temp.cleanup()

    def upload(self):
        return publish(
            self.config,
            self.request,
            self.fd,
            self.result,
            self.s3,
            expected_owner=os.getuid()
        )

    def test_external_configuration_is_private_and_requires_exact_schema(self):
        env = {
            'FPGA_CI_BACKEND': 'ssm',
            'FPGA_CI_CONFIG_JSON': json.dumps(self.config),
            'RUNNER_TEMP': str(self.directory),
            'GITHUB_ENV': str(self.directory / 'github.env')
        }
        target = configure(env)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(target.read_text()), self.config)
        self.assertEqual((self.directory / 'github.env').read_text(),
                         'CORALNPU_ACTIVATION_CONFIG=' + str(target) + '\n')
        with self.assertRaises(ValueError):
            configure(
                env | {
                    'FPGA_CI_CONFIG_JSON':
                    json.dumps(
                        self.config | {'credential': 'must-not-be-accepted'}
                    )
                }
            )

    def test_external_disabled_configuration_creates_no_file(self):
        env = {
            'FPGA_CI_BACKEND': 'ssm',
            'FPGA_CI_CONFIG_JSON':
            json.dumps(self.config | {'enabled': False}),
            'RUNNER_TEMP': str(self.directory),
            'GITHUB_ENV': str(self.directory / 'github.env')
        }
        with self.assertRaises(ValueError):
            configure(env)
        self.assertFalse((self.directory / 'coralnpu-activation.json').exists()
                         )

    def test_wire_roundtrip_and_reject_bearer_or_mismatch(self):
        parameters = encode_request(self.request, self.config)
        env = {'SSM_' + key: value[0] for key, value in parameters.items()}
        self.assertEqual(
            decode_ssm_environment(env, self.config), self.request
        )
        with self.assertRaises(ValueError):
            decode_ssm_environment(
                env | {'SSM_SourceSha': 'b' * 40}, self.config
            )
        bad = copy.deepcopy(self.request)
        bad['upload_url'] = 'https://example.invalid/bearer'
        with self.assertRaises(ValueError):
            encode_request(bad, self.config)
        bad = copy.deepcopy(self.request)
        bad['eligibility']['source_ref'] = 'refs/heads/main'
        with self.assertRaises(ValueError):
            validate_request(bad, self.config)

    def test_duplicate_json_field_is_rejected(self):
        payload = json.dumps(
            self.request
        ).replace('"run_id": "123"', '"run_id": "9", "run_id": "123"').encode()
        env = {
            'SSM_' + k: v[0]
            for k, v in encode_request(self.request, self.config).items()
        }
        env['SSM_RequestBase64'] = base64.b64encode(payload).decode()
        env['SSM_RequestSha256'] = hashlib.sha256(payload).hexdigest()
        with self.assertRaises(ValueError):
            decode_ssm_environment(env, self.config)

    def test_upload_generates_receipt_last_and_refuses_collision(self):
        receipt = self.upload()
        self.assertEqual(
            self.s3.calls[-1], ('put', 'coralnpu/ci/123/1/receipt.json')
        )
        self.assertEqual(receipt['source_ref'], 'refs/pull/11/head')
        self.assertEqual(receipt['source_run_head_sha'], SHA)
        with self.assertRaises(RuntimeError):
            self.upload()
        self.assertEqual(
            json.loads(self.s3.objects['coralnpu/ci/123/1/receipt.json']),
            receipt
        )

    def test_disabled_config_makes_no_aws_or_metadata_calls(self):
        self.config['enabled'] = False
        with self.assertRaises(ValueError):
            self.upload()
        with self.assertRaises(ValueError):
            create_host_s3(
                self.config,
                urlopen=lambda *a, **k: self.fail('metadata must not be read')
            )
        self.assertEqual(self.s3.calls, [])

    def test_no_network_before_all_artifacts_validate(self):
        (self.inbox / 'evidence.tar').chmod(0o600)
        with self.assertRaises(ValueError):
            self.upload()
        self.assertEqual(self.s3.calls, [])

    def test_symlink_hardlink_and_fifo_fail_before_upload(self):
        path = self.inbox / 'checkpoint.tar'
        path.unlink()
        target = self.directory / 'sentinel'
        target.write_bytes(b'private')
        target.chmod(0o400)
        path.symlink_to(target)
        with self.assertRaises((OSError, ValueError)):
            self.upload()
        path.unlink()
        os.link(target, path)
        with self.assertRaises(ValueError):
            self.upload()
        path.unlink()
        os.mkfifo(path, 0o400)
        with self.assertRaises(ValueError):
            self.upload()
        self.assertEqual(self.s3.calls, [])
        self.assertEqual(target.read_bytes(), b'private')

    def test_failure_upload_preserves_only_evidence_and_unqualified_receipt(
        self
    ):
        (self.inbox / 'checkpoint.tar').unlink()
        self.result.update(qualified=False, status='failed')
        receipt = self.upload()
        self.assertFalse(receipt['qualified'])
        self.assertNotIn('checkpoint_bytes', receipt)
        self.assertEqual(len(self.s3.objects), 2)

    def test_imdsv2_role_mismatch_blocks_before_credentials(self):
        calls = []

        def opener(request, timeout):
            calls.append(request)
            return io.BytesIO(
                b'fake-test-token' if len(calls) == 1 else b'wrong-role'
            )

        with self.assertRaises(ValueError):
            create_host_s3(self.config, urlopen=opener, sdk_check=lambda: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].method, 'PUT')
        self.assertIn('X-aws-ec2-metadata-token', calls[1].headers)

    def test_end_to_end_controller_ssm_decode_host_upload_receipt(self):
        test = self
        document = json.loads(
            (ROOT / 'ssm/CoralNpuFpgaCiBuild.json').read_text()
        )

        class FakeSSM:

            class exceptions:

                class InvocationDoesNotExist(Exception):
                    pass

            def describe_document(self, **kwargs):
                return {
                    'Document': {
                        'HashType': 'Sha256',
                        'Hash': test.config['document_aws_sha256'],
                        'DocumentVersion': '1'
                    }
                }

            def get_document(self, **kwargs):
                return {
                    'Content': json.dumps(document),
                    'DocumentVersion': '1'
                }

            def send_command(self, **kwargs):
                test.assertEqual(
                    kwargs['InstanceIds'], [test.config['instance_id']]
                )
                test.assertNotIn('ServiceRoleArn', kwargs)
                request = decode_ssm_environment({
                    'SSM_' + k: v[0]
                    for k, v in kwargs['Parameters'].items()
                }, test.config)
                test.assertEqual(request, test.request)
                test.upload()
                return {
                    'Command': {
                        'CommandId': '12345678-1234-1234-1234-123456789abc'
                    }
                }

            def get_command_invocation(self, **kwargs):
                return {'Status': 'Success'}

        output = self.directory / 'download'
        result = submit_and_collect(
            self.config, self.proof, lambda _: self.proof, FakeSSM(), self.s3,
            '123', '1', output, document
        )
        self.assertEqual(result['receipt']['source_sha'], SHA)
        self.assertEqual((output / 'checkpoint.tar').read_bytes(),
                         (self.inbox / 'checkpoint.tar').read_bytes())

    def test_fork_approval_rechecked_after_document_pin_before_send(self):
        test = self
        document = {}

        class FakeSSM:

            def describe_document(self, **kwargs):
                return {
                    'Document': {
                        'HashType': 'Sha256',
                        'Hash': test.config['document_aws_sha256'],
                        'DocumentVersion': '1'
                    }
                }

            def get_document(self, **kwargs):
                return {'Content': '{}', 'DocumentVersion': '1'}

            def send_command(self, **kwargs):
                test.fail('approval changed: must not send')

        changed = self.proof | {'exact_sha_approvers': []}
        with self.assertRaises(ValueError):
            submit_and_collect(
                self.config, self.proof, lambda _: changed, FakeSSM(), self.s3,
                '123', '1', self.directory / 'download', document
            )
        self.assertEqual(self.s3.calls, [])

    def test_wrong_document_hash_fails_without_request(self):
        test = self

        class FakeSSM:

            def describe_document(self, **kwargs):
                return {
                    'Document': {
                        'HashType': 'Sha256',
                        'Hash': 'e' * 64,
                        'DocumentVersion': '1'
                    }
                }

            def get_document(self, **kwargs):
                test.fail('must reject wrong hash first')

        with self.assertRaises(ValueError):
            submit_and_collect(
                self.config, self.proof, lambda _: self.proof, FakeSSM(),
                self.s3, '123', '1', self.directory / 'download', {}
            )

    def test_oidc_endpoint_and_claim_mismatch_do_not_reach_aws(self):
        with self.assertRaises(ValueError):
            oidc_token(
                self.config,
                {'ACTIONS_ID_TOKEN_REQUEST_URL': 'http://169.254.169.254/'},
                opener=lambda *a, **k: self.
                fail('invalid endpoint must not be fetched')
            )
        claims = {
            'aud': 'sts.amazonaws.com',
            'sub': 'wrong',
            'repository_id': str(self.config['repository_id']),
            'ref': 'refs/heads/main'
        }
        token = 'e30.' + base64.urlsafe_b64encode(
            json.dumps(claims).encode()
        ).decode().rstrip('=') + '.dGVzdA'
        with self.assertRaises(ValueError):
            oidc_token(
                self.config, {
                    'ACTIONS_ID_TOKEN_REQUEST_URL':
                    'https://pipelines.actions.githubusercontent.com/token',
                    'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'test-only'
                },
                opener=lambda *a, **k: io.
                BytesIO(json.dumps({
                    'value': token
                }).encode())
            )

    def cancellation_client(
        self, *, uncertain=False, cancel_fails=False, pending=False
    ):
        test = self

        class FakeSSM:
            operations = []

            class exceptions:

                class InvocationDoesNotExist(Exception):
                    pass

            def describe_document(self, **kwargs):
                return {
                    'Document': {
                        'HashType': 'Sha256',
                        'Hash': test.config['document_aws_sha256'],
                        'DocumentVersion': '1'
                    }
                }

            def get_document(self, **kwargs):
                return {'Content': '{}', 'DocumentVersion': '1'}

            def send_command(self, **kwargs):
                operation = kwargs['Parameters']['Operation'][0]
                self.operations.append(operation)
                request = decode_ssm_environment({
                    'SSM_' + key: value[0]
                    for key, value in kwargs['Parameters'].items()
                }, test.config)
                test.assertEqual(request, test.request)
                if operation == 'Cancel' and cancel_fails:
                    raise ConnectionError('injected cancelled transport')
                if operation == 'Build' and uncertain:
                    raise TimeoutError('injected uncertain delivery')
                return {
                    'Command': {
                        'CommandId': '12345678-1234-1234-1234-123456789abc'
                    }
                }

            def get_command_invocation(self, **kwargs):
                if pending:
                    return {'Status': 'InProgress'}
                raise KeyboardInterrupt()

        return FakeSSM()

    def test_interruption_sends_only_exact_fixed_cancel(self):
        ssm = self.cancellation_client()
        with self.assertRaises(KeyboardInterrupt):
            submit_and_collect(
                self.config, self.proof, lambda _: self.proof, ssm, self.s3,
                '123', '1', self.directory / 'download', {}
            )
        self.assertEqual(ssm.operations, ['Build', 'Cancel'])
        self.assertEqual(self.s3.calls, [])

    def test_unknown_send_outcome_is_cancelled_without_new_admission(self):
        ssm = self.cancellation_client(uncertain=True)
        with self.assertRaises(TimeoutError):
            submit_and_collect(
                self.config, self.proof, lambda _: self.proof, ssm, self.s3,
                '123', '1', self.directory / 'download', {}
            )
        self.assertEqual(ssm.operations, ['Build', 'Cancel'])

    def test_cancel_transport_failure_preserves_original_error_and_watchdog_fallback(
        self
    ):
        ssm = self.cancellation_client(cancel_fails=True)
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(KeyboardInterrupt):
            submit_and_collect(
                self.config, self.proof, lambda _: self.proof, ssm, self.s3,
                '123', '1', self.directory / 'download', {}
            )
        self.assertEqual(ssm.operations, ['Build', 'Cancel'])
        self.assertIn(
            'independent host deadline remains the fallback', output.getvalue()
        )

    def test_polling_is_bounded_with_fake_clock(self):
        ssm = self.cancellation_client(pending=True)
        now = [0]

        def sleep(seconds):
            now[0] += seconds

        with self.assertRaises(KeyError):
            submit_and_collect(
                self.config,
                self.proof,
                lambda _: self.proof,
                ssm,
                self.s3,
                '123',
                '1',
                self.directory / 'download', {},
                monotonic=lambda: now[0],
                sleep=sleep
            )
        self.assertEqual(now[0], 22260)
        self.assertEqual(ssm.operations, ['Build', 'Cancel'])


if __name__ == '__main__':
    unittest.main()
