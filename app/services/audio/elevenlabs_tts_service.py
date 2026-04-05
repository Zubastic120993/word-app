"""ElevenLabs Text-to-Speech service with local caching."""

import logging
from typing import Optional

import httpx

from app.config import settings
from app.services.audio.murf_tts_service import (
    normalize_text_for_audio,
    compute_audio_hash,
    get_audio_file_path,
    get_relative_audio_path,
    ensure_audio_directory,
    AudioGenerationError,
)

logger = logging.getLogger(__name__)


class ElevenLabsDisabledError(Exception):
    """Raised when ElevenLabs is disabled but audio is requested."""
    pass


class ElevenLabsInvalidConfigurationError(Exception):
    """Raised when ElevenLabs configuration is invalid (e.g., invalid voice ID)."""
    pass


class ElevenLabsTTSService:
    """
    ElevenLabs Text-to-Speech service.
    
    Generates audio pronunciations via ElevenLabs API and caches them locally.
    
    Usage:
        service = ElevenLabsTTSService()
        audio_bytes = service.generate_audio("Hello, world!")
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        language: Optional[str] = None,
    ):
        """
        Initialize ElevenLabs TTS service.
        
        Args:
            api_key: ElevenLabs API key. Falls back to settings.elevenlabs_api_key.
            voice: Voice ID. Falls back to settings.elevenlabs_voice_pl.
            model: Model ID. Falls back to settings.elevenlabs_model.
            language: Language code. Defaults to "pl" for Polish.
        """
        self.api_key = api_key or settings.elevenlabs_api_key
        self.voice = voice or settings.elevenlabs_voice_pl
        self.model = model or settings.elevenlabs_model
        self.language = language or "pl"  # Default to Polish
        self.engine = "elevenlabs"
        
        # ElevenLabs API base URL
        self.api_base_url = "https://api.elevenlabs.io/v1"
        
    def is_enabled(self) -> bool:
        """Check if ElevenLabs TTS is enabled and configured."""
        return settings.elevenlabs_enabled and bool(self.api_key) and bool(self.voice)
    
    def generate_audio(self, text: str) -> bytes:
        """
        Generate audio pronunciation via ElevenLabs API.
        
        Args:
            text: Text to convert to speech.
            
        Returns:
            MP3 audio bytes.
            
        Raises:
            ElevenLabsDisabledError: If ElevenLabs is disabled.
            AudioGenerationError: If API call fails.
        """
        if not self.is_enabled():
            raise ElevenLabsDisabledError("ElevenLabs TTS is disabled or not configured")
        
        if not text or not text.strip():
            raise AudioGenerationError("Cannot generate audio for empty text")
        
        try:
            # Call ElevenLabs API to generate audio
            audio_bytes = self._call_elevenlabs_api(text)
            return audio_bytes
            
        except (ElevenLabsDisabledError, ElevenLabsInvalidConfigurationError):
            raise
        except (httpx.ConnectError, httpx.NetworkError, httpx.TimeoutException) as e:
            # Network connectivity issues (DNS, connection refused, timeout)
            error_msg = str(e)
            if "nodename nor servname provided" in error_msg or "Name or service not known" in error_msg:
                logger.error(f"ElevenLabs API DNS resolution failed: {e}")
                raise AudioGenerationError(
                    "Failed to connect to ElevenLabs API: DNS resolution failed. "
                    "Please check your internet connection and network settings."
                )
            elif "Connection refused" in error_msg or "Connection reset" in error_msg:
                logger.error(f"ElevenLabs API connection refused: {e}")
                raise AudioGenerationError(
                    "Failed to connect to ElevenLabs API: Connection refused. "
                    "The API server may be down or unreachable."
                )
            elif "timeout" in error_msg.lower():
                logger.error(f"ElevenLabs API request timeout: {e}")
                raise AudioGenerationError(
                    "Failed to connect to ElevenLabs API: Request timed out. "
                    "Please check your internet connection."
                )
            else:
                logger.error(f"ElevenLabs API network error: {e}")
                raise AudioGenerationError(
                    f"Failed to connect to ElevenLabs API: Network error. {error_msg}"
                )
        except httpx.HTTPError as e:
            logger.error(f"ElevenLabs API HTTP error: {e}")
            raise AudioGenerationError(f"Failed to connect to ElevenLabs API: {e}")
        except Exception as e:
            logger.error(f"ElevenLabs audio generation error: {e}")
            raise AudioGenerationError(f"Failed to generate audio: {e}")
    
    def _call_elevenlabs_api(self, text: str) -> bytes:
        """
        Call ElevenLabs API to generate speech.
        
        Args:
            text: Text to convert to speech.
            
        Returns:
            Audio bytes (MP3 format).
            
        Raises:
            AudioGenerationError: If API call fails.
        """
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        
        payload = {
            "text": text,
            "model_id": self.model,
        }
        
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(
                    f"{self.api_base_url}/text-to-speech/{self.voice}",
                    json=payload,
                    headers=headers,
                )
        except (httpx.ConnectError, httpx.NetworkError, httpx.TimeoutException) as e:
            # Re-raise network errors to be handled by the outer exception handler
            raise
        
        # Process response (only reached if no network error occurred)
        if response.status_code != 200:
            error_detail = response.text[:500] if response.text else "Unknown error"
            logger.error(f"ElevenLabs API error {response.status_code}: {error_detail}")
            
            # Parse error response for better error messages
            if response.status_code == 400:
                try:
                    error_data = response.json()
                    error_message = error_data.get("detail", {}).get("message", "") or error_data.get("message", "")
                    
                    # Check for invalid voice ID
                    if "voice" in error_message.lower() or "invalid" in error_message.lower():
                        raise ElevenLabsInvalidConfigurationError(
                            f"Invalid voice ID '{self.voice}'. {error_message} "
                            f"Please check available voices at: https://elevenlabs.io/app/voices"
                        )
                    
                    # Generic 400 error with message
                    if error_message:
                        raise ElevenLabsInvalidConfigurationError(
                            f"ElevenLabs API configuration error: {error_message}"
                        )
                except (ValueError, KeyError, AttributeError):
                    # If JSON parsing fails, fall through to generic error
                    pass
            
            if response.status_code == 401 or response.status_code == 403:
                raise ElevenLabsInvalidConfigurationError(
                    f"Invalid or missing ElevenLabs API key: {error_detail}"
                )
            
            # Generic error for other status codes or parsing failures
            raise AudioGenerationError(
                f"ElevenLabs API returned status {response.status_code}. "
                f"Error: {error_detail}"
            )
        
        # ElevenLabs returns audio bytes directly
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
        
        Hash includes engine, voice, language, and normalized text.
        Note: Model is not included in hash as it's part of the voice configuration.
        
        Args:
            text: Text to hash.
            
        Returns:
            Computed audio hash.
        """
        normalized = normalize_text_for_audio(text)
        return compute_audio_hash(self.engine, self.voice, self.language, normalized)
