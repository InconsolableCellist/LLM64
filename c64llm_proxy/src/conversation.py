"""Conversation management and storage"""

import json
import time
from datetime import datetime
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
        conv_id = int(time.time())  # Unix timestamp (32-bit safe)
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

    def save_checkpoint(self, name: str = '') -> str:
        """Snapshot the current conversation (adventure save points)."""
        if not self.current_conversation:
            return ''
        cpdir = self.data_dir / 'checkpoints'
        cpdir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        label = name.strip() or stamp
        path = cpdir / f"{self.current_id}_{stamp}.json"
        with open(path, 'w') as f:
            json.dump({'name': label, 'saved_at': stamp,
                       'conversation': self.current_conversation}, f, indent=2)
        return label

    def list_checkpoints(self):
        """Checkpoints for the current conversation, oldest first."""
        cpdir = self.data_dir / 'checkpoints'
        out = []
        for p in sorted(cpdir.glob(f"{self.current_id}_*.json")):
            try:
                with open(p) as f:
                    d = json.load(f)
                out.append({'path': p, 'name': d.get('name', p.stem),
                            'saved_at': d.get('saved_at', ''),
                            'messages': len(d['conversation']['messages'])})
            except Exception:
                continue
        return out

    def restore_checkpoint(self, index: int) -> str:
        """Replace the current conversation with checkpoint #index (1-based).
        Returns the checkpoint name, or '' if not found."""
        cps = self.list_checkpoints()
        if not 1 <= index <= len(cps):
            return ''
        with open(cps[index - 1]['path']) as f:
            d = json.load(f)
        self.current_conversation = d['conversation']
        self.current_id = self.current_conversation['id']
        self.save()
        return cps[index - 1]['name']

    def set_meta(self, key: str, value):
        """Attach session state (mode, music) to the conversation."""
        if self.current_conversation is not None:
            self.current_conversation.setdefault('meta', {})[key] = value

    def get_meta(self, key: str, default=None):
        if not self.current_conversation:
            return default
        return self.current_conversation.get('meta', {}).get(key, default)

    def get_messages(self) -> List[Dict]:
        """Get messages for API (role/content only)"""
        if not self.current_conversation:
            return []
        return [
            {'role': msg['role'], 'content': msg['content']}
            for msg in self.current_conversation['messages']
        ]

    def set_title(self, title: str, auto: bool = False):
        """Set the conversation title (auto=True marks it LLM-generated)"""
        if not self.current_conversation:
            return
        self.current_conversation['title'] = title
        if auto:
            self.current_conversation['auto_titled'] = True
        self.save()
        self.logger.info(f"Conversation titled: {title}")

    def save(self):
        """Save current conversation to disk (Open WebUI format)"""
        if not self.current_conversation:
            return

        filename = f"{self.current_id}.json"
        filepath = self.data_dir / filename

        # Open WebUI compatible format (+ our 'meta' extension: mode and
        # music state, so loading a conversation can restore both)
        data = {
            'id': str(self.current_id),
            'title': self.current_conversation['title'],
            'auto_titled': self.current_conversation.get('auto_titled', False),
            'created_at': self.current_conversation['created_at'],
            'updated_at': self.current_conversation['updated_at'],
            'meta': self.current_conversation.get('meta', {}),
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
                'auto_titled': data.get('auto_titled', False),
                'created_at': data['created_at'],
                'updated_at': data['updated_at'],
                'meta': data.get('meta', {}),
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
                        'timestamp': data['updated_at'],
                        'message_count': len(data['chat']['messages'])
                    })
            except Exception as e:
                self.logger.error(f"Error loading {filepath}: {e}")

        # Sort by timestamp, newest first
        conversations.sort(key=lambda x: x['timestamp'], reverse=True)

        # Limit to 50 most recent
        return conversations[:50]
