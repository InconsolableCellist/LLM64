"""Conversation management and storage"""

import json
import time
from pathlib import Path
from typing import List, Dict, Optional
import logging


class ConversationManager:
    """Manages conversation storage and retrieval"""

    def __init__(self, config, client_id: Optional[int] = None):
        self.config = config
        self.client_id = client_id or 0
        self.data_dir = Path(config.data_dir) / 'conversations'
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.current_conversation = None
        self.current_id = None
        self.logger = logging.getLogger(f"ConversationManager-{self.client_id}")

    def new_conversation(self) -> int:
        """Start a new conversation"""
        conv_id = int(time.time() * 1000)  # Millisecond precision for uniqueness
        self.current_id = conv_id
        self.current_conversation = {
            'id': conv_id,
            'created_at': int(time.time()),
            'updated_at': int(time.time()),
            'title': 'New Conversation',
            'messages': []
        }
        self.logger.info(f"Started new conversation: {conv_id}")
        return conv_id

    def add_message(self, role: str, content: str):
        """Add message to current conversation"""
        if not self.current_conversation:
            self.new_conversation()

        message = {
            'role': role,
            'content': content,
            'timestamp': int(time.time())
        }
        self.current_conversation['messages'].append(message)
        self.current_conversation['updated_at'] = int(time.time())

        # Auto-generate title from first user message
        if role == 'user' and len(self.current_conversation['messages']) == 1:
            title = content[:40].strip()
            if len(content) > 40:
                title += '...'
            self.current_conversation['title'] = title

        self.logger.debug(f"Added {role} message: {content[:50]}...")

    def get_messages(self) -> List[Dict]:
        """Get messages for API (role/content only)"""
        if not self.current_conversation:
            return []
        return [
            {'role': msg['role'], 'content': msg['content']}
            for msg in self.current_conversation['messages']
        ]

    def save(self):
        """Save current conversation to disk (Open WebUI format)"""
        if not self.current_conversation:
            return

        filename = f"{self.current_id}.json"
        filepath = self.data_dir / filename

        # Open WebUI compatible format
        data = {
            'id': str(self.current_id),
            'title': self.current_conversation['title'],
            'created_at': self.current_conversation['created_at'],
            'updated_at': self.current_conversation['updated_at'],
            'chat': {
                'messages': self.current_conversation['messages']
            }
        }

        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)

        self.logger.info(f"Saved conversation: {filepath}")

    def load_conversation(self, conv_id: int) -> bool:
        """Load a conversation from disk"""
        filename = f"{conv_id}.json"
        filepath = self.data_dir / filename

        if not filepath.exists():
            self.logger.warning(f"Conversation not found: {conv_id}")
            return False

        try:
            with open(filepath, 'r') as f:
                data = json.load(f)

            self.current_id = conv_id
            self.current_conversation = {
                'id': conv_id,
                'title': data['title'],
                'created_at': data['created_at'],
                'updated_at': data['updated_at'],
                'messages': data['chat']['messages']
            }

            self.logger.info(f"Loaded conversation: {conv_id}")
            return True

        except Exception as e:
            self.logger.error(f"Error loading conversation {conv_id}: {e}")
            return False

    def list_conversations(self) -> List[Dict]:
        """List all conversations (sorted by updated_at, newest first)"""
        conversations = []

        for filepath in self.data_dir.glob('*.json'):
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                    conversations.append({
                        'id': int(data['id']),
                        'title': data['title'],
                        'timestamp': data['updated_at']
                    })
            except Exception as e:
                self.logger.error(f"Error loading {filepath}: {e}")

        # Sort by timestamp, newest first
        conversations.sort(key=lambda x: x['timestamp'], reverse=True)

        # Limit to 50 most recent
        return conversations[:50]
