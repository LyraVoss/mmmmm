import os
import pathlib
from bot.utils.logger import get_logger

log = get_logger(__name__)

class EvolutionManager:
    """Handles Mymm's self-improvement and code analysis."""
    
    def __init__(self, root_dir: str):
        self.root_dir = pathlib.Path(root_dir)
        self.blacklist = [".env", "credentials.py", "__pycache__", ".git", ".venv"]
        self.proposals = []

    def get_editable_files(self):
        """Returns a list of files Mymm is allowed to analyze and improve."""
        files = []
        for path in self.root_dir.rglob("*.py"):
            if not any(b in str(path) for b in self.blacklist):
                files.append(str(path.relative_to(self.root_dir)))
        return files

    def read_source(self, filename: str) -> str:
        """Reads source code, strictly blocking blacklisted files."""
        if any(b in filename for b in self.blacklist) or ".env" in filename:
            log.error(f"🛑 Security Breach: Mymm attempted to access {filename}")
            raise PermissionError("Access to credentials or environment files is strictly prohibited.")
        
        full_path = self.root_dir / filename
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()

    async def analyze_performance(self, metrics: dict):
        """
        Simulates Mymm 'thinking' about her code based on recent metrics.
        In a real integration, this would call the Gemini API.
        """
        log.info("🧠 Mymm is analyzing her own code for potential evolutions...")
        
        # Example: If win rate is low, suggest a change to brain.py
        if metrics.get("win_rate", 0) < 0.2:
            self.proposals.append({
                "id": len(self.proposals) + 1,
                "file": "bot/strategy/brain.py",
                "reason": "Win rate is low. Suggesting more aggressive EP conservation.",
                "diff": "- ep_rest_threshold = 4\n+ ep_rest_threshold = 6",
                "status": "pending"
            })

    def apply_proposal(self, proposal_id: int):
        """Applies an approved code change."""
        # Logic to apply patch/diff safely would go here
        for p in self.proposals:
            if p["id"] == proposal_id:
                p["status"] = "applied"
                log.info(f"🧬 Evolution applied: {p['reason']}")
                return True
        return False

    def get_status(self):
        return {
            "editable_files": self.get_editable_files(),
            "proposals": self.proposals[-5:], # Show last 5
            "security_lock": "ACTIVE (.env protected)"
        }