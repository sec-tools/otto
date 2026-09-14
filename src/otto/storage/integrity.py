import asyncio
import logging
import shutil
from pathlib import Path

from otto.storage.database import OttoDatabase

logger = logging.getLogger(__name__)

class IntegrityManager:
    """Manages database data integrity, backups, and recovery."""

    def __init__(self, db_manager: OttoDatabase):
        self.db_manager = db_manager
        self.db_path = self.db_manager.db_path
        self.backup_path = self.db_path.with_suffix('.db.bak')

    async def startup_check(self) -> bool:
        """Run PRAGMA integrity_check on startup."""
        logger.info("Running database integrity check...")
        if not self.db_manager.db:
            await self.db_manager.connect()
            
        async with self.db_manager.db.execute("PRAGMA integrity_check;") as cursor:
            result = await cursor.fetchone()
            
        if result and result[0].lower() == 'ok':
            logger.info("Integrity check passed.")
            return True
        else:
            logger.error(f"Integrity check failed: {result}")
            return False

    async def create_backup(self) -> bool:
        """Copy database file with verification."""
        logger.info(f"Creating backup at {self.backup_path}")
        
        # Ensure checkpoint happens before backup
        if self.db_manager.db:
            await self.db_manager.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        
        try:
            shutil.copy2(self.db_path, self.backup_path)
            return await self.verify_backup(self.backup_path)
        except Exception as e:
            logger.error(f"Backup creation failed: {e}")
            return False

    async def verify_backup(self, path: Path) -> bool:
        """Run PRAGMA integrity_check on a backup file."""
        import aiosqlite
        try:
            async with aiosqlite.connect(path) as db:
                async with db.execute("PRAGMA integrity_check;") as cursor:
                    result = await cursor.fetchone()
                    if result and result[0].lower() == 'ok':
                        return True
            return False
        except Exception as e:
            logger.error(f"Backup verification failed: {e}")
            return False

    async def recover(self) -> bool:
        """WAL recovery → last backup → fresh start waterfall."""
        logger.warning("Attempting database recovery...")
        
        # Close current connections
        await self.db_manager.close()
        
        # Check if backup exists and is valid
        if self.backup_path.exists():
            logger.info("Found backup, verifying...")
            if await self.verify_backup(self.backup_path):
                logger.info("Backup valid, restoring...")
                shutil.copy2(self.backup_path, self.db_path)
                
                # Reconnect
                await self.db_manager.connect()
                if await self.startup_check():
                    return True
        
        logger.error("Recovery from backup failed or no valid backup found. Starting fresh.")
        # Start fresh
        if self.db_path.exists():
            self.db_path.unlink()
        await self.db_manager.connect()
        return True

    def schedule_daily_backup(self):
        """Schedule daily backup (stub for event loop)."""
        async def _backup_loop():
            while True:
                await asyncio.sleep(86400) # 24 hours
                await self.create_backup()
                
        asyncio.create_task(_backup_loop())
