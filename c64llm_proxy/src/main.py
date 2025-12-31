"""C64 LLM Proxy Server - Main Entry Point"""

import asyncio
import argparse
import logging
import sys
from pathlib import Path

from .tcp_server import C64Server
from .api_client import APIClient
from .config import Config


def setup_logging(verbose: bool = False):
    """Configure logging"""
    level = logging.DEBUG if verbose else logging.INFO

    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


async def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='C64 LLM Proxy Server - Bridge C64 to OpenAI APIs'
    )
    parser.add_argument(
        '--host',
        default='0.0.0.0',
        help='Bind address (default: 0.0.0.0)'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=6400,
        help='TCP port (default: 6400)'
    )
    parser.add_argument(
        '--config',
        default='config.toml',
        help='Config file path (default: config.toml)'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )
    args = parser.parse_args()

    # Setup logging
    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("C64 LLM Proxy Server v0.1.0")
    logger.info("=" * 60)

    # Load configuration
    try:
        config = Config(args.config if Path(args.config).exists() else None)
        logger.info(f"API Endpoint: {config.api_base_url}")
        logger.info(f"Model: {config.model}")
        logger.info(f"Data Directory: {config.data_dir}")
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        logger.error("\nPlease set OPENAI_API_KEY environment variable or create config.toml")
        logger.error("Example: export OPENAI_API_KEY=sk-your-key-here")
        sys.exit(1)

    # Initialize components
    api_client = APIClient(config)
    server = C64Server(args.host, args.port, config, api_client)

    try:
        # Run server (this blocks in event loop)
        logger.info(f"Starting server on {args.host}:{args.port}")
        logger.info("Press Ctrl+C to stop")
        logger.info("=" * 60)
        await server.run()

    except KeyboardInterrupt:
        logger.info("\nShutdown requested...")

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

    finally:
        logger.info("Cleaning up...")
        await server.close()
        await api_client.close()
        logger.info("Goodbye!")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # Already handled in main()
