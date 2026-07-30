"""Unit tests for media extraction utilities.

Tests detect_asset_type(), and the fallback behaviour of
sample_video_frame_data_urls() and extract_video_audio_transcript() when their
optional dependencies (cv2, moviepy) are unavailable.  All tests are offline.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from sentinel.models import Case
from sentinel.tools.media_utils import detect_asset_type, quarantine

# ---------------------------------------------------------------------------
# detect_asset_type
# ---------------------------------------------------------------------------

class DetectAssetTypeTests:
    def test_mp4_returns_video(self):
        assert detect_asset_type("clip.mp4") == "video"

    def test_jpg_returns_image(self):
        assert detect_asset_type("photo.jpg") == "image"

    def test_png_returns_image(self):
        assert detect_asset_type("photo.png") == "image"

    def test_mp3_returns_audio(self):
        assert detect_asset_type("sound.mp3") == "audio"

    def test_wav_returns_audio(self):
        assert detect_asset_type("sound.wav") == "audio"

    def test_txt_returns_text(self):
        assert detect_asset_type("document.txt") == "text"

    def test_synthetic_returns_text(self):
        assert detect_asset_type("fixture.synthetic") == "text"

    def test_unknown_extension_falls_back_to_text(self):
        # An unrecognised extension should fall back to 'text', not raise.
        assert detect_asset_type("file.unknownext") == "text"

    def test_metadata_asset_type_overrides_extension(self):
        # If caller passes asset_type in metadata it takes precedence.
        result = detect_asset_type("file.txt", {"asset_type": "image"})
        assert result == "image"

    def test_empty_string_path_falls_back_to_text(self):
        assert detect_asset_type("") == "text"


# ---------------------------------------------------------------------------
# sample_video_frame_data_urls — cv2 ImportError fallback
# ---------------------------------------------------------------------------

class VideoFrameSamplingTests:
    def test_returns_empty_list_when_cv2_not_installed(self):
        """When cv2 is missing the function must return [] without raising."""
        from sentinel.tools.production_analysis import sample_video_frame_data_urls

        # Temporarily hide cv2 from the import machinery.
        original = sys.modules.get("cv2")
        sys.modules["cv2"] = None  # type: ignore[assignment]
        try:
            result = sample_video_frame_data_urls("nonexistent.mp4")
        finally:
            if original is None:
                sys.modules.pop("cv2", None)
            else:
                sys.modules["cv2"] = original

        assert result == []

    def test_returns_empty_list_when_file_not_opened(self):
        """cv2 available but VideoCapture fails to open → return []."""
        from sentinel.tools.production_analysis import sample_video_frame_data_urls

        mock_capture = MagicMock()
        mock_capture.isOpened.return_value = False

        mock_cv2 = MagicMock()
        mock_cv2.VideoCapture.return_value = mock_capture

        with patch.dict(sys.modules, {"cv2": mock_cv2}):
            result = sample_video_frame_data_urls("bad.mp4")

        assert result == []


# ---------------------------------------------------------------------------
# extract_video_audio_transcript — moviepy ImportError fallback
# ---------------------------------------------------------------------------

class VideoTranscriptExtractionTests:
    def _make_case(self, path: str) -> Case:
        return Case(id="test-video", asset_type="video", asset_path=path, metadata={})

    def test_returns_empty_string_when_moviepy_not_installed(self):
        """When moviepy is missing the function must return '' without raising."""
        from sentinel.tools.production_analysis import extract_video_audio_transcript

        # Hide both moviepy import paths.
        with patch.dict(sys.modules, {"moviepy": None, "moviepy.editor": None}):  # type: ignore[dict-item]
            result = extract_video_audio_transcript(self._make_case("fake.mp4"), client=MagicMock())

        assert result == ""

    def test_returns_empty_string_on_clip_open_error(self):
        """Any exception during clip processing must be swallowed and return ''."""
        from sentinel.tools.production_analysis import extract_video_audio_transcript

        mock_clip_cls = MagicMock(side_effect=RuntimeError("cannot open file"))
        mock_moviepy = MagicMock()
        mock_moviepy.VideoFileClip = mock_clip_cls

        with patch.dict(sys.modules, {"moviepy": mock_moviepy}):
            result = extract_video_audio_transcript(self._make_case("bad.mp4"), client=MagicMock())

        assert result == ""

    def test_returns_empty_string_when_audio_track_missing(self):
        """Video with no audio track (clip.audio is None) returns ''."""
        from sentinel.tools.production_analysis import extract_video_audio_transcript

        mock_clip = MagicMock()
        mock_clip.audio = None
        mock_clip.__enter__ = MagicMock(return_value=mock_clip)
        mock_clip.__exit__ = MagicMock(return_value=False)

        mock_clip_cls = MagicMock(return_value=mock_clip)
        mock_moviepy = MagicMock()
        mock_moviepy.VideoFileClip = mock_clip_cls

        with patch.dict(sys.modules, {"moviepy": mock_moviepy}):
            result = extract_video_audio_transcript(self._make_case("silent.mp4"), client=MagicMock())

        assert result == ""


class QuarantineTests:
    def test_production_asset_is_moved_out_of_upload_storage(self, tmp_path: Path):
        source = tmp_path / "uploads" / "asset.txt"
        source.parent.mkdir()
        source.write_text("sensitive", encoding="utf-8")
        quarantine_dir = tmp_path / "quarantine"
        case = Case(
            id="case-1",
            asset_type="text",
            asset_path=str(source),
            metadata={"analysis_mode": "production", "moderation_run_id": "run-1"},
        )

        assert quarantine(case, quarantine_dir)
        assert not source.exists()
        assert (quarantine_dir / "run-1-asset.txt").read_text(encoding="utf-8") == "sensitive"

    def test_missing_asset_is_not_reported_as_quarantined(self, tmp_path: Path):
        case = Case("missing", "text", str(tmp_path / "missing.txt"), {"analysis_mode": "production"})

        assert not quarantine(case, tmp_path / "quarantine")
