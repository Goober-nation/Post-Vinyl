"""
ListenBrainzFeedback — Concrete implementation of FeedbackService using ListenBrainz API.

Submits love/hate feedback to ListenBrainz.
"""

from typing import Optional
import requests

from app.config import Config
from app.exceptions import (
    ListenBrainzConnectionError,
    ListenBrainzFeedbackError,
    ListenBrainzDisabledError
)
from app.logging_config import get_logger
from app.services.interfaces.feedback import FeedbackService, SyncResult
from app.services.library import Song

logger = get_logger(__name__)


class ListenBrainzFeedback(FeedbackService):
    """
    ListenBrainz-based feedback implementation.
    
    Submits love (+1) and hate (-1) feedback to ListenBrainz API.
    """
    
    def __init__(self, config: Config):
        """
        Initialize ListenBrainzFeedback.
        
        Args:
            config: Config object with ListenBrainz settings
        """
        self.config = config
        self.base_url = config.listenbrainz.url
        self.token = config.listenbrainz.token
        self.username = config.listenbrainz.username
        self.session = requests.Session()
    
    def _get_headers(self) -> dict:
        """Get HTTP headers for ListenBrainz API."""
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        return headers
    
    def send_feedback(self, mbid: str, score: int) -> bool:
        """
        Send love (+1) or hate (-1) feedback for a recording.
        
        Args:
            mbid: MusicBrainz recording ID
            score: +1 for love, -1 for hate
            
        Returns:
            True if successful, False otherwise
        """
        if not self.config.listenbrainz.enabled:
            raise ListenBrainzDisabledError()
        
        if score not in (1, -1):
            raise ValueError(f"Score must be +1 or -1, got {score}")
        
        if not mbid:
            logger.warning("Cannot send feedback: no MBID provided")
            return False
        
        logger.info(f"Sending feedback: mbid={mbid}, score={score}")
        
        url = f"{self.base_url}/1/feedback/recording-feedback"
        payload = {"recording_mbid": mbid, "score": score}
        
        try:
            resp = self.session.post(
                url,
                json=payload,
                headers=self._get_headers(),
                timeout=30
            )
            
            if resp.status_code == 200:
                logger.info(f"Feedback sent successfully")
                return True
            else:
                logger.error(f"Feedback failed: HTTP {resp.status_code} - {resp.text}")
                return False
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Feedback connection error: {e}")
            raise ListenBrainzConnectionError(self.base_url, str(e))
    
    def sync_loves(self, starred: list[Song]) -> SyncResult:
        """
        Sync Navidrome starred songs to ListenBrainz as loves.
        
        Args:
            starred: List of Song objects from LibraryService.get_starred()
            
        Returns:
            SyncResult with synced count and failures
        """
        if not self.config.listenbrainz.enabled:
            raise ListenBrainzDisabledError()
        
        logger.info(f"Syncing {len(starred)} starred songs as loves")
        
        synced_count = 0
        failed_count = 0
        failures = []
        
        for song in starred:
            if not song.mbid:
                logger.debug(f"Skipping {song.title}: no MBID")
                continue
            
            try:
                success = self.send_feedback(song.mbid, 1)
                
                if success:
                    synced_count += 1
                else:
                    failed_count += 1
                    failures.append({
                        "song_id": song.song_id,
                        "mbid": song.mbid,
                        "message": "Feedback submission failed"
                    })
                    
            except Exception as e:
                logger.error(f"Failed to sync love for {song.title}: {e}")
                failed_count += 1
                failures.append({
                    "song_id": song.song_id,
                    "mbid": song.mbid,
                    "message": str(e)
                })
        
        logger.info(f"Love sync complete: {synced_count} synced, {failed_count} failed")
        
        return SyncResult(
            synced_count=synced_count,
            failed_count=failed_count,
            failures=failures
        )
    
    def sync_hates(self, trashed: list[Song]) -> SyncResult:
        """
        Sync Navidrome trash playlist to ListenBrainz as hates.
        
        Args:
            trashed: List of Song objects from Trash playlist
            
        Returns:
            SyncResult with synced count and failures
        """
        if not self.config.listenbrainz.enabled:
            raise ListenBrainzDisabledError()
        
        logger.info(f"Syncing {len(trashed)} trashed songs as hates")
        
        synced_count = 0
        failed_count = 0
        failures = []
        
        for song in trashed:
            if not song.mbid:
                logger.debug(f"Skipping {song.title}: no MBID")
                continue
            
            try:
                success = self.send_feedback(song.mbid, -1)
                
                if success:
                    synced_count += 1
                else:
                    failed_count += 1
                    failures.append({
                        "song_id": song.song_id,
                        "mbid": song.mbid,
                        "message": "Feedback submission failed"
                    })
                    
            except Exception as e:
                logger.error(f"Failed to sync hate for {song.title}: {e}")
                failed_count += 1
                failures.append({
                    "song_id": song.song_id,
                    "mbid": song.mbid,
                    "message": str(e)
                })
        
        logger.info(f"Hate sync complete: {synced_count} synced, {failed_count} failed")
        
        return SyncResult(
            synced_count=synced_count,
            failed_count=failed_count,
            failures=failures
        )
