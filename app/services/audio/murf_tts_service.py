"""Murf AI Text-to-Speech service with local caching."""

import hashlib
import logging
import re
import unicodedata
from pathlib import Path
from typing import Optional, List, Dict, Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class AudioGenerationError(Exception):
    """Raised when audio generation fails."""
    pass


class MurfDisabledError(Exception):
    """Raised when Murf is disabled but audio is requested."""
    pass


class MurfInvalidConfigurationError(Exception):
    """Raised when Murf configuration is invalid (e.g., invalid voice ID)."""
    pass


def normalize_text_for_audio(text: str) -> str:
    """
    Normalize text for consistent audio hash generation.
    
    Normalization rules:
    - Unicode NFC normalization (compose characters)
    - Lowercase
    - Strip leading/trailing whitespace
    - Collapse multiple whitespace to single space
    - Keep punctuation (affects pronunciation)
    
    Args:
        text: Raw text to normalize.
        
    Returns:
        Normalized text for hashing.
    """
    if not text:
        return ""
    
    # Unicode NFC normalization
    normalized = unicodedata.normalize("NFC", text)
    
    # Lowercase
    normalized = normalized.lower()
    
    # Strip leading/trailing whitespace
    normalized = normalized.strip()
    
    # Collapse multiple whitespace to single space
    normalized = re.sub(r"\s+", " ", normalized)
    
    return normalized


def compute_audio_hash(
    engine: str,
    voice: str,
    language: str,
    normalized_text: str,
) -> str:
    """
    Compute deterministic hash for audio caching.
    
    Hash is based on:
    - TTS engine (e.g., "murf")
    - Voice ID (e.g., "en-US-marcus")
    - Language code (e.g., "en-US")
    - Normalized text content
    
    Args:
        engine: TTS engine identifier.
        voice: Voice ID.
        language: Language code.
        normalized_text: Pre-normalized text.
        
    Returns:
        SHA-256 hash (first 16 characters for filename friendliness).
    """
    # Create deterministic string for hashing
    hash_input = f"{engine}|{voice}|{language}|{normalized_text}"
    
    # Compute SHA-256 hash
    full_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
    
    # Return first 16 characters for reasonable filename length
    return full_hash[:16]


def ensure_audio_directory() -> Path:
    """
    Ensure the audio storage directory exists.
    
    Creates data/audio/ directory if it doesn't exist.
    
    Returns:
        Path to audio directory.
    """
    audio_dir = settings.audio_dir
    audio_dir.mkdir(parents=True, exist_ok=True)
    return audio_dir


def get_audio_file_path(audio_hash: str, language: str, voice: str) -> Path:
    """
    Generate the file path for an audio file.
    
    Format: data/audio/{hash}_{language}_{voice}.mp3
    
    Args:
        audio_hash: Computed hash of audio content.
        language: Language code.
        voice: Voice ID.
        
    Returns:
        Absolute path to audio file.
    """
    # Sanitize voice for filename (replace special chars)
    safe_voice = re.sub(r"[^a-zA-Z0-9_-]", "_", voice)
    safe_language = re.sub(r"[^a-zA-Z0-9_-]", "_", language)
    
    filename = f"{audio_hash}_{safe_language}_{safe_voice}.mp3"
    return ensure_audio_directory() / filename


def get_relative_audio_path(audio_hash: str, language: str, voice: str) -> str:
    """
    Generate the relative file path for database storage.
    
    Format: data/audio/{hash}_{language}_{voice}.mp3
    
    Args:
        audio_hash: Computed hash of audio content.
        language: Language code.
        voice: Voice ID.
        
    Returns:
        Relative path string for database storage.
    """
    # Sanitize voice for filename (replace special chars)
    safe_voice = re.sub(r"[^a-zA-Z0-9_-]", "_", voice)
    safe_language = re.sub(r"[^a-zA-Z0-9_-]", "_", language)
    
    filename = f"{audio_hash}_{safe_language}_{safe_voice}.mp3"
    return f"data/audio/{filename}"


class MurfTTSService:
    """
    Murf AI Text-to-Speech service.
    
    Generates audio pronunciations via Murf API and caches them locally.
    
    Usage:
        service = MurfTTSService()
        audio_bytes = service.generate_audio("Hello, world!")
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        voice: Optional[str] = None,
        language: Optional[str] = None,
    ):
        """
        Initialize Murf TTS service.
        
        Args:
            api_key: Murf API key. Falls back to settings.murf_api_key.
            voice: Voice ID. Falls back to settings.murf_voice.
            language: Language code. Falls back to settings.murf_language.
        """
        self.api_key = api_key or settings.murf_api_key
        self.voice = voice or settings.murf_voice
        self.language = language or settings.murf_language
        self.engine = "murf"
        
        # Murf API base URL
        self.api_base_url = "https://api.murf.ai/v1"
        
    def is_enabled(self) -> bool:
        """Check if Murf TTS is enabled and configured."""
        return settings.murf_enabled and bool(self.api_key)
    
    def get_available_voices(self) -> List[Dict[str, Any]]:
        """
        Get available Murf voices filtered by configured language.
        
        Calls GET https://api.murf.ai/v1/speech/voices and filters results
        by language matching settings.murf_language.
        
        Returns:
            List of voice dictionaries with:
            - voice_id: str
            - gender: str | None
            - style: str | None
            
        Raises:
            MurfDisabledError: If Murf is disabled.
            MurfInvalidConfigurationError: If API key is missing or invalid.
            AudioGenerationError: If API call fails.
        """
        if not settings.murf_enabled:
            raise MurfDisabledError("Murf TTS is disabled")
        
        if not self.api_key:
            raise MurfInvalidConfigurationError("Murf API key is missing")
        
        headers = {
            "api-key": self.api_key,
        }
        
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(
                    f"{self.api_base_url}/speech/voices",
                    headers=headers,
                )
                
                if response.status_code != 200:
                    error_detail = response.text[:500] if response.text else "Unknown error"
                    logger.error(f"Murf API error {response.status_code}: {error_detail}")
                    
                    if response.status_code == 401 or response.status_code == 403:
                        raise MurfInvalidConfigurationError(
                            f"Invalid or missing Murf API key: {error_detail}"
                        )
                    
                    raise AudioGenerationError(
                        f"Murf API returned status {response.status_code}. "
                        f"Error: {error_detail}"
                    )
                
                data = response.json()
                
                # Parse response - handle both list and object with voices array
                if isinstance(data, list):
                    voices = data
                elif isinstance(data, dict) and "voices" in data:
                    voices = data["voices"]
                elif isinstance(data, dict) and "data" in data:
                    voices = data["data"]
                else:
                    logger.warning(f"Unexpected Murf API response format: {type(data)}")
                    voices = []
                
                # Filter by language and extract simplified format
                filtered_voices = []
                for voice in voices:
                    if not isinstance(voice, dict):
                        continue
                    
                    # Check if voice matches the configured language
                    voice_language = voice.get("language") or voice.get("languageCode")
                    if voice_language != self.language:
                        continue
                    
                    # Extract voice_id (could be voiceId, voice_id, or id)
                    voice_id = (
                        voice.get("voiceId") or 
                        voice.get("voice_id") or 
                        voice.get("id") or 
                        voice.get("voiceId")
                    )
                    
                    if not voice_id:
                        continue
                    
                    # Extract gender and style (may be None)
                    gender = voice.get("gender") or voice.get("voiceGender")
                    style = voice.get("style") or voice.get("voiceStyle")
                    
                    filtered_voices.append({
                        "voice_id": str(voice_id),
                        "gender": str(gender) if gender else None,
                        "style": str(style) if style else None,
                    })
                
                return filtered_voices
                
        except (MurfDisabledError, MurfInvalidConfigurationError):
            raise
        except httpx.HTTPError as e:
            logger.error(f"Murf API HTTP error: {e}")
            raise AudioGenerationError(f"Failed to connect to Murf API: {e}")
        except Exception as e:
            logger.error(f"Murf voice discovery error: {e}")
            raise AudioGenerationError(f"Failed to fetch available voices: {e}")
    
    def generate_audio(self, text: str) -> bytes:
        """
        Generate audio pronunciation via Murf API.
        
        Args:
            text: Text to convert to speech.
            
        Returns:
            MP3 audio bytes.
            
        Raises:
            MurfDisabledError: If Murf is disabled.
            AudioGenerationError: If API call fails.
        """
        if not self.is_enabled():
            raise MurfDisabledError("Murf TTS is disabled or not configured")
        
        if not text or not text.strip():
            raise AudioGenerationError("Cannot generate audio for empty text")
        
        try:
            # Call Murf API to generate audio
            audio_url = self._call_murf_api(text)
            
            # Download the audio file
            audio_bytes = self._download_audio(audio_url)
            
            return audio_bytes
            
        except (MurfDisabledError, MurfInvalidConfigurationError):
            raise
        except httpx.HTTPError as e:
            logger.error(f"Murf API HTTP error: {e}")
            raise AudioGenerationError(f"Failed to connect to Murf API: {e}")
        except Exception as e:
            logger.error(f"Murf audio generation error: {e}")
            raise AudioGenerationError(f"Failed to generate audio: {e}")
    
    def _call_murf_api(self, text: str) -> str:
        """
        Call Murf API to generate speech.
        
        Args:
            text: Text to convert to speech.
            
        Returns:
            URL of generated audio file.
            
        Raises:
            AudioGenerationError: If API call fails.
        """
        headers = {
            "Content-Type": "application/json",
            "api-key": self.api_key,
        }
        
        payload = {
            "text": text,
            "voiceId": self.voice,
            "format": "MP3",
            "sampleRate": 44100,
        }
        
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                f"{self.api_base_url}/speech/generate",
                json=payload,
                headers=headers,
            )
            
            if response.status_code != 200:
                error_detail = response.text[:500] if response.text else "Unknown error"
                logger.error(f"Murf API error {response.status_code}: {error_detail}")
                
                # Parse error response for better error messages
                if response.status_code == 400:
                    try:
                        error_data = response.json()
                        error_message = error_data.get("errorMessage", "")
                        
                        # Check for invalid voice ID
                        if "voice_id" in error_message.lower() or "invalid voice" in error_message.lower():
                            raise MurfInvalidConfigurationError(
                                f"Invalid voice ID '{self.voice}'. {error_message} "
                                f"Please check available voices at: https://murf.ai/api/docs/voices-styles/voice-library"
                            )
                        
                        # Generic 400 error with message
                        raise MurfInvalidConfigurationError(
                            f"Murf API configuration error: {error_message}"
                        )
                    except (ValueError, KeyError):
                        # If JSON parsing fails, fall through to generic error
                        pass
                
                # Generic error for other status codes or parsing failures
                raise AudioGenerationError(
                    f"Murf API returned status {response.status_code}. "
                    f"Error: {error_detail}"
                )
            
            data = response.json()
            
            # Extract audio URL from response
            audio_url = data.get("audioFile")
            if not audio_url:
                raise AudioGenerationError("Murf API response missing audioFile URL")
            
            return audio_url
    
    def _download_audio(self, audio_url: str) -> bytes:
        """
        Download audio file from URL.
        
        Args:
            audio_url: URL of audio file.
            
        Returns:
            Audio bytes.
            
        Raises:
            AudioGenerationError: If download fails.
        """
        with httpx.Client(timeout=30.0) as client:
            response = client.get(audio_url)
            
            if response.status_code != 200:
                raise AudioGenerationError(
                    f"Failed to download audio: status {response.status_code}"
                )
            
            return response.content
    
    def save_audio_file(
        self,
        audio_bytes: bytes,
        audio_hash: str,
    ) -> str:
        """
        Save audio bytes to local file.
        
        Args:
            audio_bytes: MP3 audio data.
            audio_hash: Pre-computed hash for filename.
            
        Returns:
            Relative file path (for database storage).
        """
        file_path = get_audio_file_path(audio_hash, self.language, self.voice)
        relative_path = get_relative_audio_path(audio_hash, self.language, self.voice)
        
        # Write audio file
        file_path.write_bytes(audio_bytes)
        logger.info(f"Saved audio file: {relative_path}")
        
        return relative_path
    
    def get_audio_hash_for_text(self, text: str) -> str:
        """
        Compute audio hash for given text.
        
        Args:
            text: Text to hash.
            
        Returns:
            Computed audio hash.
        """
        normalized = normalize_text_for_audio(text)
        return compute_audio_hash(self.engine, self.voice, self.language, normalized)
