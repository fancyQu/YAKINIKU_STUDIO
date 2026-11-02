import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from yakiniku.provider import ProviderError
from yakiniku.server import TRANSLATION_WORKERS, create_app
from yakiniku.storage import SessionStore, SettingsStore


class RetryConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_runs_three_at_once_and_closes_after_all_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            SettingsStore(root).save({"api_key": "test-key",
                "api_base_url": "https://example.com/v1", "translation_model": "test-model"})
            store = SessionStore(root / "recordings")
            session = store.create("fixture")
            session["status"] = "completed"
            session["duration_ms"] = 6000
            session["segments"] = [{"id": str(i), "start_ms": i * 1000,
                "end_ms": (i + 1) * 1000, "ja": str(i), "zh": ""} for i in range(6)]
            store.save(session)
            active = 0
            maximum = 0
            completed = []
            closed = []

            class Translator:
                def __init__(self, **kwargs):
                    pass

                async def translate(self, text, context=None):
                    nonlocal active, maximum
                    active += 1
                    maximum = max(maximum, active)
                    await asyncio.sleep(0.02 if text == "0" else 0.005)
                    active -= 1
                    completed.append(text)
                    if text == "2":
                        raise ProviderError("fixture failure")
                    return "译" + text

                async def close(self):
                    closed.append((active, len(completed)))

            app = create_app(root, translator_factory=Translator)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1:8765") as client:
                response = await client.post(f"/api/sessions/{session['id']}/retry-translations")
                self.assertEqual(response.status_code, 502)
                saved = store.load(session["id"])
                self.assertEqual(maximum, TRANSLATION_WORKERS)
                self.assertEqual(len(completed), 6)
                self.assertEqual(closed, [(0, 6)])
                self.assertNotEqual(completed[0], "0")
                self.assertEqual([s["start_ms"] for s in saved["segments"]], list(range(0, 6000, 1000)))
                self.assertEqual(saved["segments"][2]["translation_status"], "failed")
                self.assertTrue(all(s["zh"] for s in saved["segments"] if s["id"] != "2"))
                # A failed retry releases its lock, allowing the remaining sentence to retry.
                self.assertEqual((await client.post(f"/api/sessions/{session['id']}/retry-translations")).status_code, 502)


if __name__ == "__main__":
    unittest.main()
