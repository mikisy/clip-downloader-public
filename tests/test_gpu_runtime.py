import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clip_downloader import analysis


@unittest.skipUnless(os.name == 'nt', 'Windows DLL loading')
class GpuRuntimeTests(unittest.TestCase):
    def test_packaged_dlls_are_loaded_by_absolute_path_and_registered_once(self):
        importlib.import_module("ctranslate2")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bin').mkdir()
            for name in ('cublas64_12.dll', 'cublasLt64_12.dll', 'cudnn64_9.dll'):
                (root / 'bin' / name).touch()
            spec = SimpleNamespace(submodule_search_locations=[str(root)])
            with patch.dict(analysis._cuda_directories, {}, clear=True), \
                 patch.dict(os.environ), \
                 patch('clip_downloader.analysis.importlib.util.find_spec', return_value=spec), \
                 patch('clip_downloader.analysis.os.add_dll_directory') as register, \
                 patch('clip_downloader.analysis.ctypes.WinDLL') as load, \
                 patch('ctranslate2.get_cuda_device_count', return_value=1):
                self.assertEqual(analysis.select_device('cuda', lambda _: None), 'cuda')
                self.assertEqual(analysis.select_device('auto', lambda _: None), 'cuda')
                register.assert_called_once_with(str((root / 'bin').resolve()))
                self.assertTrue(all(Path(call.args[0]).is_absolute() for call in load.call_args_list))

    def test_cpu_does_not_load_cuda(self):
        with patch('clip_downloader.analysis.prepare_cuda_dlls') as prepare:
            self.assertEqual(analysis.select_device('cpu', lambda _: None), 'cpu')
            prepare.assert_not_called()
