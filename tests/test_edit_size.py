"""Keep edit auto sizing intact from client jobs to the server sampling graph."""
import base64
import importlib
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import uuid

from comfy_workflow_api import WorkflowAPI


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'edit_size_client'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT/'hermes/plugins/comfy-workflows')]
sys.modules[PACKAGE] = package
jobs = importlib.import_module(PACKAGE+'.jobs')
worker = importlib.import_module(PACKAGE+'.worker')
transport = importlib.import_module(PACKAGE+'.transport')


class Context:
    def get_config(self, key, default=None):
        return default


class CompileTransport(transport.Transport):
    """Replace SSH with actual server compilation, without queueing generation."""
    def call(self, packet):
        self.packet = packet
        images = []
        for index, blob in enumerate(packet['images']):
            path = self.data_dir/f'uploaded-{index}.png'
            path.write_bytes(base64.b64decode(blob, validate=True))
            images.append(str(path))
        api = WorkflowAPI(ROOT/'ubuntu')
        self.graph = api.compile(packet['workflow'], {**packet['request'], 'images': images})['prompt']
        return {'success': True, 'result': {'task_id': str(uuid.uuid4()), 'state': 'queued'}}


class EditSizeContractTests(unittest.TestCase):
    def test_auto_edit_follows_encoder_latent_for_paths_and_prior_outputs(self):
        origin = {'profile': 'test', 'platform': 'telegram', 'bot': '123',
                  'chat_id': '10', 'user_id': '20'}
        reference = ROOT/'ubuntu/examples/robot.png'
        for preset in ('fast', 'quality'):
            for use_image_id in (False, True):
                with self.subTest(preset=preset, use_image_id=use_image_id), \
                        tempfile.TemporaryDirectory() as temporary, \
                        patch.object(worker, 'launch'), \
                        patch(PACKAGE+'.local_delivery.prepare_origin', side_effect=lambda ctx, value: value):
                    service = jobs.JobService(Context(), Path(temporary))
                    image = str(reference)
                    if use_image_id:
                        prior = service.create({'prompt': 'A geometric robot'}, origin)['task_id']
                        artifact = service.store.register_outputs(prior, [{'path': str(reference)}])[0]
                        image = artifact['image_id']
                    task = service.create({'prompt': 'Recolor the geometric robot blue.',
                                           'images': [image], 'preset': preset, 'size': 'auto'}, origin)
                    bridge = CompileTransport(Context(), Path(temporary))
                    runner = worker.Runner(service.store, task['task_id'], transport=bridge,
                                           runtime=object())
                    self.assertTrue(runner.submit())
                    self.assertEqual(bridge.packet['request']['output_size'], {'mode': 'follow_image'})
                    self.assertEqual(base64.b64decode(bridge.packet['images'][0]), reference.read_bytes())
                    graph = bridge.graph
                    self.assertFalse(graph['468']['inputs']['switch'])
                    self.assertEqual(graph['468']['inputs']['on_false'], ['474', 2])
                    self.assertEqual(graph['474']['inputs']['resolution'], 1024)
                    self.assertEqual(graph['458']['inputs']['latent_image'], ['468', 0])
                    if preset == 'fast':
                        self.assertEqual(graph['pruna_sigmas']['inputs']['latent'],
                                         graph['458']['inputs']['latent_image'])


if __name__ == '__main__':
    unittest.main()
