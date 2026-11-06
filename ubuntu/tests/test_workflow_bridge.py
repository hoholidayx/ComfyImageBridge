import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from comfy_workflow_api import workflow_bridge as bridge


class RemoteContractTest(unittest.TestCase):
    def test_unverified_generation_is_rejected_without_submitting(self):
        module=SimpleNamespace(status=lambda:{'http_healthy':True,'ready':False})
        spec=SimpleNamespace(loader=Mock())
        with patch.object(bridge,'WorkflowAPI'),patch.object(bridge.importlib.util,'spec_from_file_location',return_value=spec),patch.object(bridge.importlib.util,'module_from_spec',return_value=module),patch.object(bridge.batches,'submit') as submit:
            with self.assertRaisesRegex(ValueError,'No generation was submitted'):
                bridge.invoke({'root':'/unused','operation':'submit'})
        submit.assert_not_called()

    def test_single_cancel_available_without_readiness_and_never_fetches(self):
        with tempfile.TemporaryDirectory() as temporary:
            api=Mock()
            api.cancel.return_value={'state':'cancel_requested'}
            with patch.object(bridge,'WorkflowAPI',return_value=api),patch.object(bridge.batches,'directory',return_value=Path(temporary)),patch.object(bridge.batches,'cancel') as batch_cancel:
                result=bridge.invoke({'root':temporary,'operation':'task','task_id':'one','action':'cancel'})
            self.assertEqual(result['state'],'cancel_requested')
            api.cancel.assert_called_once_with('one')
            api.fetch.assert_not_called()
            api.status.assert_not_called()
            batch_cancel.assert_not_called()

    def test_batch_cancel_only_dispatches_known_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)
            (path/'batch.json').write_text('{}')
            api=Mock()
            with patch.object(bridge,'WorkflowAPI',return_value=api),patch.object(bridge.batches,'directory',return_value=path),patch.object(bridge.batches,'cancel',return_value={'state':'cancel_requested'}) as cancel:
                bridge.invoke({'root':temporary,'operation':'task','task_id':'parent','action':'cancel'})
            cancel.assert_called_once_with(api,'parent')
            api.cancel.assert_not_called()
