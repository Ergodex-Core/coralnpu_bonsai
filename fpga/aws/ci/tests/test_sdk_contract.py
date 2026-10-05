"""Real pinned SDK model tests; Stubber prevents every AWS network request."""
import base64
from datetime import datetime, timedelta, timezone
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
sys.path[:0] = [str(ROOT), str(ROOT / 'orchestrator')]
from test_transport import config_fixture, proof_fixture
from protocol import make_request, encode_request
from transfer.host_upload import create_host_s3, publish
from controller import verify_document
try:
    import botocore
    from botocore.stub import Stubber, ANY
    from sdk_runtime import verify_sdk_runtime
    verify_sdk_runtime()
    SDK_READY = True
except (ImportError, ValueError):
    SDK_READY = False


@unittest.skipUnless(
    SDK_READY,
    'Run with requirements-controller.lock installed to validate actual SDK models'
)
class SDKContractTests(unittest.TestCase):

    def test_imdsv2_factory_ignores_ambient_credentials_and_put_model_is_valid(
        self
    ):
        config = config_fixture()
        credentials = {
            'Code':
            'Success',
            'AccessKeyId':
            'ASIATESTONLY123456789',
            'SecretAccessKey':
            'test-only-secret-not-a-real-key',
            'Token':
            'test-only-session-token',
            'Expiration':
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        }
        replies = [
            b'test-only-metadata-token',
            config['host_role_and_profile'].encode(),
            json.dumps(credentials).encode()
        ]
        requests = []

        def opener(request, timeout):
            requests.append(request)
            return io.BytesIO(replies.pop(0))
        with patch.dict(os.environ, {'AWS_CONFIG_FILE': '/do-not-read-config',
                                    'AWS_SHARED_CREDENTIALS_FILE': '/do-not-read-credentials',
                                    'AWS_PROFILE': 'do-not-load-profile'}), \
             patch('botocore.httpsession.URLLib3Session.send', side_effect=AssertionError('unexpected network')):
            client = create_host_s3(config, urlopen=opener)
            self.assertEqual(len(requests), 3)
            self.assertTrue(
                all(
                    request.full_url.
                    startswith('http://169.254.169.254/latest/')
                    for request in requests
                )
            )
            self.assertTrue(
                all(
                    'X-aws-ec2-metadata-token' in request.headers
                    for request in requests[1:]
                )
            )
            self.assertEqual(
                client.meta.endpoint_url, 'https://s3.us-east-1.amazonaws.com'
            )
            request = make_request(proof_fixture(config), config, '123', '1')
            with tempfile.TemporaryDirectory() as directory:
                for name in ['checkpoint.tar', 'evidence.tar']:
                    path = Path(directory) / name
                    path.write_bytes(name.encode())
                    path.chmod(0o400)
                fd = os.open(directory, os.O_RDONLY)
                try:
                    with Stubber(client) as stub:
                        for name in ['checkpoint.tar', 'evidence.tar',
                                     'receipt.json']:
                            expected = {
                                'Bucket': config['artifact_bucket'],
                                'Key': request['artifact_prefix'] + name,
                                'Body': ANY,
                                'ContentLength': ANY,
                                'ContentType': ANY,
                                'ServerSideEncryption': 'AES256',
                                'IfNoneMatch': '*',
                                'ExpectedBucketOwner': config['aws_account'],
                                'ChecksumSHA256': ANY
                            }
                            stub.add_response(
                                'put_object',
                                {'ResponseMetadata': {
                                    'HTTPStatusCode': 200
                                }}, expected
                            )
                        result = publish(
                            config,
                            request,
                            fd, {
                                'source_sha': request['source_sha'],
                                'source_ref':
                                request['eligibility']['source_ref'],
                                'qualified': True,
                                'status': 'success'
                            },
                            client,
                            expected_owner=os.getuid()
                        )
                        self.assertTrue(result['qualified'])
                        stub.assert_no_pending_responses()
                finally:
                    os.close(fd)

    def test_real_ssm_model_accepts_pins_and_fixed_parameters(self):
        from botocore.session import Session
        config = config_fixture()
        session = Session()
        session.set_config_variable('config_file', os.devnull)
        session.set_config_variable('credentials_file', os.devnull)
        session.set_credentials('test-key', 'test-secret', 'test-token')
        with patch('botocore.httpsession.URLLib3Session.send',
                   side_effect=AssertionError('unexpected network')):
            client = session.create_client('ssm', region_name='us-east-1')
            document = json.loads(
                (ROOT / 'ssm/CoralNpuFpgaCiBuild.json').read_text()
            )
            with Stubber(client) as stub:
                stub.add_response(
                    'describe_document', {
                        'Document': {
                            'HashType': 'Sha256',
                            'Hash': config['document_aws_sha256'],
                            'DocumentVersion': '1'
                        }
                    }, {
                        'Name': config['document_name'],
                        'DocumentVersion': '1'
                    }
                )
                stub.add_response(
                    'get_document', {
                        'Content': json.dumps(document),
                        'DocumentVersion': '1'
                    }, {
                        'Name': config['document_name'],
                        'DocumentVersion': '1',
                        'DocumentFormat': 'JSON'
                    }
                )
                verify_document(client, config, document)
                parameters = encode_request(
                    make_request(proof_fixture(config), config, '123', '1'),
                    config
                )
                args = {
                    'InstanceIds': [config['instance_id']],
                    'DocumentName': config['document_name'],
                    'DocumentVersion': '1',
                    'DocumentHash': config['document_aws_sha256'],
                    'DocumentHashType': 'Sha256',
                    'TimeoutSeconds': 600,
                    'Parameters': parameters
                }
                stub.add_response(
                    'send_command', {
                        'Command': {
                            'CommandId': '12345678-1234-1234-1234-123456789abc'
                        }
                    }, args
                )
                client.send_command(**args)
                stub.assert_no_pending_responses()


if __name__ == '__main__':
    unittest.main()
