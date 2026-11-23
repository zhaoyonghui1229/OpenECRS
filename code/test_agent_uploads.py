# -*- coding: utf-8 -*-
import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_uploads


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch_dir = mock.patch.object(agent_uploads, "UPLOAD_DIR", Path(self.tmp.name))
        self.patch_index = mock.patch.object(agent_uploads, "INDEX_FILE", Path(self.tmp.name) / "index.json")
        self.patch_dir.start(); self.patch_index.start()

    def tearDown(self):
        self.patch_dir.stop(); self.patch_index.stop(); self.tmp.cleanup()

    def test_save_parse_and_retrieve(self):
        raw = ("OpenECRS evidence note. " * 900).encode()
        record = agent_uploads.save_upload("note.txt", base64.b64encode(raw).decode())
        parsed = agent_uploads.parse_upload(record["id"])
        self.assertEqual(parsed["ext"], ".txt")
        self.assertGreater(parsed["tokens"], 0)
        self.assertIn("OpenECRS", parsed["text"])
        context = agent_uploads.build_context(record["id"], "evidence")
        self.assertIn("context", context)

    def test_rejects_bad_extension_and_size(self):
        with self.assertRaises(agent_uploads.UploadError):
            agent_uploads.save_upload("evil.exe", base64.b64encode(b"x").decode())
        with mock.patch.object(agent_uploads, "MAX_BYTES", 2):
            with self.assertRaises(agent_uploads.UploadError):
                agent_uploads.save_upload("big.txt", base64.b64encode(b"12345").decode())

    def test_rejects_missing_and_bad_id(self):
        self.assertIsNone(agent_uploads.get_upload("../etc/passwd"))
        with self.assertRaises(agent_uploads.UploadError):
            agent_uploads.parse_upload("deadbeef")

    def test_chunk_retrieval_prefers_relevant(self):
        text = ("alpha " * 900) + ("\n\nunique-keyword " * 20) + (" beta " * 900)
        record = {"mode": "rag", "text": text}
        out = agent_uploads.retrieve(record, "unique-keyword", 2)
        self.assertIn("unique-keyword", out)


if __name__ == "__main__":
    unittest.main()
