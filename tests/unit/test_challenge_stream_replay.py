from __future__ import annotations

import unittest

from engine.http_client import HttpStreamAdapter


class _ChunkResponse:
    def __init__(self, chunks: list[bytes], content_type: str) -> None:
        self.chunks = chunks
        self.headers = {"Content-Type": content_type}
        self.status_code = 403
        self.calls = 0

    def iter_content(self):
        self.calls += 1
        yield from self.chunks


class ReplayStreamTests(unittest.TestCase):
    def test_text_read_is_byte_identical_with_tiny_chunks(self) -> None:
        chunks = [b"<div class='cf-", b"turnstile'></div>", b"tail"]
        response = _ChunkResponse(chunks, "text/html")
        adapter = HttpStreamAdapter(response, "https://example.test")
        self.assertEqual(adapter.read(5) + adapter.read(), b"".join(chunks))
        self.assertTrue(adapter.challenge_verdict.automation_eligible)
        self.assertEqual(response.calls, 1)

    def test_iterator_is_byte_identical_and_bounded(self) -> None:
        body = b"<div class='cf-turnstile'></div>" + (b"x" * 70000)
        adapter = HttpStreamAdapter(_ChunkResponse([body], "text/html"), "https://example.test")
        self.assertEqual(b"".join(adapter.iter_content()), body)
        self.assertLessEqual(len(adapter._replay._buffer), 65536)
        self.assertTrue(adapter.challenge_verdict.automation_eligible)

    def test_binary_is_not_consumed_for_inspection(self) -> None:
        response = _ChunkResponse([b"\x00\x01binary"], "application/octet-stream")
        adapter = HttpStreamAdapter(response, "https://example.test")
        self.assertEqual(adapter._replay._buffer, b"")
        self.assertEqual(adapter.read(), b"\x00\x01binary")
        self.assertEqual(adapter.challenge_verdict.outcome, "binary")

    def test_interrupted_stream_returns_received_bytes_once(self) -> None:
        adapter = HttpStreamAdapter(_ChunkResponse([b"<html>", b"partial"], "text/html"), "https://example.test")
        self.assertEqual(b"".join(adapter.iter_content()), b"<html>partial")


if __name__ == "__main__":
    unittest.main()
