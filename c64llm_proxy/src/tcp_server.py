"""TCP server for C64 clients"""

import asyncio
import logging
from typing import Optional
from protocol import ProtocolHandler
from conversation import ConversationManager


class ClientHandler:
    """Handles a single C64 client connection"""

    def __init__(self, reader, writer, config, api_client, client_id):
        self.reader = reader
        self.writer = writer
        self.config = config
        self.api_client = api_client
        self.client_id = client_id
        self.logger = logging.getLogger(f"Client-{client_id}")

        # Create conversation manager and protocol handler for this client
        self.conv_manager = ConversationManager(config, client_id)
        self.protocol = ProtocolHandler(self.conv_manager, api_client)
        self.protocol.set_write_callback(self.write)

    async def handle(self):
        """Main client handler loop"""
        addr = self.writer.get_extra_info('peername')
        self.logger.info(f"Client connected from {addr}")

        try:
            while True:
                # Read one byte at a time for protocol parsing
                data = await self.reader.read(1)
                if not data:
                    # Connection closed
                    self.logger.info("Connection closed by client")
                    break

                # Pass to protocol handler
                await self.protocol.process_byte(data[0])

        except asyncio.CancelledError:
            self.logger.info("Client handler cancelled")
        except Exception as e:
            self.logger.error(f"Error handling client: {e}", exc_info=True)
        finally:
            await self.close()

    async def write(self, data: bytes):
        """Write data to client"""
        try:
            self.writer.write(data)
            await self.writer.drain()
        except Exception as e:
            self.logger.error(f"Error writing to client: {e}")
            raise

    async def close(self):
        """Close client connection"""
        self.logger.info("Closing client connection")
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


class C64Server:
    """TCP server for C64 clients"""

    def __init__(self, host: str, port: int, config, api_client):
        self.host = host
        self.port = port
        self.config = config
        self.api_client = api_client
        self.server: Optional[asyncio.Server] = None
        self.logger = logging.getLogger(__name__)
        self.client_counter = 0
        self.clients = []

    async def handle_client(self, reader, writer):
        """Handle new client connection"""
        self.client_counter += 1
        client = ClientHandler(
            reader, writer, self.config, self.api_client, self.client_counter
        )
        self.clients.append(client)

        try:
            await client.handle()
        finally:
            if client in self.clients:
                self.clients.remove(client)

    async def run(self):
        """Start TCP server"""
        self.server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )

        addr = self.server.sockets[0].getsockname()
        self.logger.info(f"Server listening on {addr[0]}:{addr[1]}")
        self.logger.info("Waiting for C64 clients...")

        async with self.server:
            await self.server.serve_forever()

    async def close(self):
        """Shutdown server"""
        self.logger.info("Shutting down server...")

        if self.server:
            self.server.close()
            await self.server.wait_closed()

        # Close all client connections
        for client in self.clients[:]:  # Copy list to avoid modification during iteration
            await client.close()

        self.logger.info("Server shut down")
