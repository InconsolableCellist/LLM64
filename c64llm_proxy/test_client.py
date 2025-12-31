#!/usr/bin/env python3
"""Mock C64 client for testing the proxy server"""

import socket
import struct
import time


class MessageType:
    """Message type codes"""
    # Client -> Server
    CHAT_REQUEST = 0x01
    CANCEL_REQUEST = 0x02
    LIST_CONVERSATIONS = 0x03
    LOAD_CONVERSATION = 0x04
    NEW_CONVERSATION = 0x05
    PING = 0x06
    ACK = 0x10
    NAK = 0x11

    # Server -> Client
    CHAT_CHUNK = 0x20
    CHAT_DONE = 0x21
    CHAT_ERROR = 0x22
    CONVERSATION_LIST = 0x23
    CONVERSATION_DATA = 0x24
    STATUS = 0x25


class MockC64Client:
    """Simulates a C64 client for testing"""

    SYNC_BYTE = 0xC6

    def __init__(self, host='localhost', port=6400):
        self.host = host
        self.port = port
        self.sock = None

    def connect(self):
        """Connect to server"""
        print(f"Connecting to {self.host}:{self.port}...")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        print("Connected!")

    def disconnect(self):
        """Disconnect from server"""
        if self.sock:
            self.sock.close()
            self.sock = None
            print("Disconnected")

    def send_message(self, msg_type, payload=b''):
        """Send a protocol message"""
        # Build frame
        frame = bytearray()
        frame.append(self.SYNC_BYTE)
        frame.append(msg_type)
        frame.append(len(payload) & 0xFF)
        frame.append((len(payload) >> 8) & 0xFF)
        frame.extend(payload)

        # Calculate and append CRC
        crc = msg_type
        crc ^= (len(payload) & 0xFF)
        crc ^= ((len(payload) >> 8) & 0xFF)
        for b in payload:
            crc ^= b
        frame.append(crc & 0xFF)

        # Send
        self.sock.sendall(bytes(frame))
        print(f"Sent: {MessageType.__dict__.get(f'name_{msg_type}', f'0x{msg_type:02X}')} ({len(payload)} bytes)")

    def receive_message(self, timeout=5.0):
        """Receive a protocol message"""
        self.sock.settimeout(timeout)

        try:
            # Read SYNC byte
            while True:
                sync = self.sock.recv(1)
                if not sync:
                    return None
                if sync[0] == self.SYNC_BYTE:
                    break

            # Read type
            msg_type = self.sock.recv(1)[0]

            # Read length (little-endian 16-bit)
            length_bytes = self.sock.recv(2)
            msg_length = length_bytes[0] | (length_bytes[1] << 8)

            # Read payload
            payload = b''
            if msg_length > 0:
                payload = self.sock.recv(msg_length)

            # Read CRC
            crc_received = self.sock.recv(1)[0]

            # Verify CRC
            crc_calc = msg_type
            crc_calc ^= (msg_length & 0xFF)
            crc_calc ^= ((msg_length >> 8) & 0xFF)
            for b in payload:
                crc_calc ^= b
            crc_calc &= 0xFF

            if crc_calc != crc_received:
                print(f"CRC mismatch! Expected 0x{crc_calc:02X}, got 0x{crc_received:02X}")
                return None

            return (msg_type, payload)

        except socket.timeout:
            print("Timeout waiting for message")
            return None

    def send_chat(self, message):
        """Send a chat request"""
        payload = message.encode('ascii') + b'\x00'
        self.send_message(MessageType.CHAT_REQUEST, payload)

        # Wait for ACK
        msg = self.receive_message()
        if msg and msg[0] == MessageType.ACK:
            print("Got ACK")

        # Receive chunks
        full_response = ""
        while True:
            msg = self.receive_message(timeout=30.0)
            if not msg:
                break

            msg_type, payload = msg

            if msg_type == MessageType.STATUS:
                status = payload.rstrip(b'\x00').decode('ascii', errors='replace')
                print(f"Status: {status}")

            elif msg_type == MessageType.CHAT_CHUNK:
                seq = payload[0]
                text = payload[1:].rstrip(b'\x00').decode('ascii', errors='replace')
                full_response += text
                print(f"Chunk {seq}: {text}", end='', flush=True)

            elif msg_type == MessageType.CHAT_DONE:
                seq, total_len = struct.unpack('<BH', payload)
                print(f"\nDone! (seq={seq}, total={total_len} bytes)")
                break

            elif msg_type == MessageType.CHAT_ERROR:
                error = payload.rstrip(b'\x00').decode('ascii', errors='replace')
                print(f"Error: {error}")
                break

        return full_response

    def ping(self):
        """Send a ping"""
        self.send_message(MessageType.PING)
        msg = self.receive_message()
        if msg and msg[0] == MessageType.ACK:
            print("Pong!")
            return True
        return False

    def list_conversations(self):
        """List conversations"""
        self.send_message(MessageType.LIST_CONVERSATIONS)

        # Wait for ACK
        msg = self.receive_message()
        if msg and msg[0] == MessageType.ACK:
            print("Got ACK")

        # Receive conversation list
        conversations = []
        while True:
            msg = self.receive_message()
            if not msg:
                break

            msg_type, payload = msg

            if msg_type == MessageType.CONVERSATION_LIST:
                count = payload[0]
                more = payload[1]
                offset = 2

                print(f"Received {count} conversations (more={more})")

                for i in range(count):
                    conv_id = struct.unpack('<I', payload[offset:offset+4])[0]
                    offset += 4
                    timestamp = struct.unpack('<I', payload[offset:offset+4])[0]
                    offset += 4

                    # Find null terminator for title
                    title_end = payload.find(b'\x00', offset)
                    title = payload[offset:title_end].decode('ascii', errors='replace')
                    offset = title_end + 1

                    conversations.append({
                        'id': conv_id,
                        'timestamp': timestamp,
                        'title': title
                    })
                    print(f"  {conv_id}: {title}")

                if more == 0:
                    break

        return conversations


def main():
    """Test the protocol"""
    print("=" * 60)
    print("Mock C64 Client - Protocol Test")
    print("=" * 60)

    client = MockC64Client()

    try:
        client.connect()

        # Test 1: Ping
        print("\n--- Test 1: Ping ---")
        client.ping()

        # Test 2: New conversation
        print("\n--- Test 2: New Conversation ---")
        client.send_message(MessageType.NEW_CONVERSATION)
        msg = client.receive_message()
        if msg and msg[0] == MessageType.ACK:
            print("New conversation created!")

        # Test 3: Send a chat message
        print("\n--- Test 3: Chat Request ---")
        print("Sending: 'Hello! Can you count to 5?'")
        response = client.send_chat("Hello! Can you count to 5?")
        print(f"\nFull response ({len(response)} bytes):")
        print(response)

        # Test 4: List conversations
        print("\n--- Test 4: List Conversations ---")
        convs = client.list_conversations()
        print(f"Found {len(convs)} conversations")

        print("\n" + "=" * 60)
        print("All tests completed!")
        print("=" * 60)

    except ConnectionRefusedError:
        print("ERROR: Could not connect to server!")
        print("Make sure the proxy server is running:")
        print("  python -m src.main")

    except KeyboardInterrupt:
        print("\nInterrupted by user")

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

    finally:
        client.disconnect()


if __name__ == '__main__':
    main()
