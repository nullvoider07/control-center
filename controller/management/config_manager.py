"""Configuration management for Control Center CLI tool"""

import os
import json
from pathlib import Path
from typing import Optional, Dict, Any
import logging

# Set up logging
logger = logging.getLogger(__name__)

# Custom exception for configuration errors
class ConfigurationError(Exception):
    """Configuration-related errors"""
    pass

# Main configuration manager class
class ConfigManager:
    """
    Manages CLI configuration with priority:
    1. CLI flags (highest priority)
    2. Environment variables
    3. Config file
    4. Defaults (lowest priority)
    """
    
    # Default configuration
    DEFAULTS = {
        'server_port': 50051,
        'timeout': 30,
        'log_level': 'INFO',
        'use_ssl': False,
    }
    
    # Config file location
    if os.name == 'nt':  # Windows
        CONFIG_DIR = Path(os.getenv('APPDATA', '~')) / 'control-center'
    else:  # Linux/macOS
        CONFIG_DIR = Path.home() / '.config' / 'control-center'
    
    CONFIG_FILE = CONFIG_DIR / 'config.json'
    
    def __init__(self):
        self.config: Dict[str, Any] = {}
        self._ensure_config_dir()
    
    # Ensure config directory exists with proper permissions
    def _ensure_config_dir(self):
        """Create config directory if it doesn't exist"""
        try:
            self.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            # Set restrictive permissions (owner only)
            if os.name != 'nt':
                os.chmod(self.CONFIG_DIR, 0o700)
        except Exception as e:
            logger.warning(f"Could not create config directory: {e}")
    
    # Load configuration from file, merging with defaults
    def load(self) -> Dict[str, Any]:
        """Load configuration from file"""
        if not self.CONFIG_FILE.exists():
            logger.debug(f"Config file not found: {self.CONFIG_FILE}")
            return self.DEFAULTS.copy()
        
        try:
            with open(self.CONFIG_FILE, 'r') as f:
                file_config = json.load(f)
            
            # Merge with defaults
            config = self.DEFAULTS.copy()
            config.update(file_config)
            
            self.config = config
            logger.debug(f"Loaded config from {self.CONFIG_FILE}")
            return config
            
        except json.JSONDecodeError as e:
            raise ConfigurationError(f"Invalid JSON in config file: {e}")
        except Exception as e:
            raise ConfigurationError(f"Failed to load config: {e}")
    
    # Save configuration to file, merging with existing config
    def save(self, config: Dict[str, Any]):
        """Save configuration to file"""
        try:
            # Merge with existing config
            existing = self.load()
            existing.update(config)
            
            with open(self.CONFIG_FILE, 'w') as f:
                json.dump(existing, f, indent=2)
            
            # Set restrictive permissions (owner only)
            if os.name != 'nt':
                os.chmod(self.CONFIG_FILE, 0o600)
            
            logger.info(f"Saved config to {self.CONFIG_FILE}")
            
        except Exception as e:
            raise ConfigurationError(f"Failed to save config: {e}")
    
    # Get API token with priority: CLI > Env Var > Config File
    def get_token(
        self,
        cli_token: Optional[str] = None,
        env_var: str = 'CONTROL_CENTER_TOKEN'
    ) -> Optional[str]:
        """
        Get API token with priority:
        1. CLI argument (highest)
        2. Environment variable
        3. Config file
        
        Args:
            cli_token: Token from CLI flag
            env_var: Environment variable name
            
        Returns:
            Token string or None
        """
        # 1. CLI flag (highest priority)
        if cli_token:
            logger.debug("Using token from CLI flag")
            return cli_token
        
        # 2. Environment variable
        env_token = os.getenv(env_var)
        if env_token:
            logger.debug(f"Using token from {env_var} environment variable")
            return env_token
        
        # 3. Config file
        config = self.load()
        file_token = config.get('api_token')
        if file_token:
            logger.debug("Using token from config file")
            return file_token
        
        return None
    
    # Get server configuration with priority: CLI > Config File > Defaults
    def get_server_config(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        use_ssl: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Get server configuration with priority:
        CLI args > Config file > Defaults
        """
        config = self.load()
        
        return {
            'host': host or config.get('server_host'),
            'port': port or config.get('server_port', self.DEFAULTS['server_port']),
            'use_ssl': use_ssl if use_ssl is not None else config.get('use_ssl', self.DEFAULTS['use_ssl']),
            'timeout': config.get('timeout', self.DEFAULTS['timeout']),
        }
    
    # Setters for configuration values
    def set_token(self, token: str):
        """Save API token to config file"""
        config = self.load()
        config['api_token'] = token
        self.save(config)
        logger.info("API token saved to config")
    
    # Set default server settings
    def set_server(self, host: str, port: int = 50051):
        """Save default server settings"""
        config = self.load()
        config['server_host'] = host
        config['server_port'] = port
        self.save(config)
        logger.info(f"Default server set to {host}:{port}")
    
    # Clear API token from config
    def clear_token(self):
        """Remove API token from config"""
        config = self.load()
        if 'api_token' in config:
            del config['api_token']
            self.save(config)
            logger.info("API token cleared from config")
    
    # Get all configuration values for display (masking sensitive info)
    def get_all(self) -> Dict[str, Any]:
        """Get all configuration (for display)"""
        config = self.load()
        
        # Mask token for security
        display_config = config.copy()
        if 'api_token' in display_config:
            token = display_config['api_token']
            if len(token) > 8:
                display_config['api_token'] = f"{token[:4]}...{token[-4:]}"
            else:
                display_config['api_token'] = "***"
        
        return display_config
    
    # Validate configuration values
    def validate(self) -> tuple[bool, list[str]]:
        """
        Validate configuration
        
        Returns:
            (is_valid, list_of_errors)
        """
        errors = []
        config = self.load()
        
        # Check for token
        if not config.get('api_token') and not os.getenv('CONTROL_CENTER_TOKEN'):
            errors.append(
                "No API token configured. Set via:\n"
                "  1. --token flag\n"
                "  2. CONTROL_CENTER_TOKEN environment variable\n"
                "  3. config file: control-center config set-token <token>"
            )
        
        # Check for server host
        if not config.get('server_host'):
            errors.append("No default server configured")
        
        # Validate port
        port = config.get('server_port', self.DEFAULTS['server_port'])
        if not isinstance(port, int) or port < 1 or port > 65535:
            errors.append(f"Invalid port: {port}")
        
        # Validate timeout
        timeout = config.get('timeout', self.DEFAULTS['timeout'])
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            errors.append(f"Invalid timeout: {timeout}")
        
        return (len(errors) == 0, errors)
    
    # Reset configuration to defaults
    def reset(self):
        """Reset configuration to defaults"""
        if self.CONFIG_FILE.exists():
            # Backup current config
            backup_file = self.CONFIG_FILE.with_suffix('.json.bak')
            import shutil
            shutil.copy2(self.CONFIG_FILE, backup_file)
            logger.info(f"Backed up config to {backup_file}")
        
        # Save defaults
        self.save(self.DEFAULTS.copy())
        logger.info("Configuration reset to defaults")
    
    # Get config file location
    @staticmethod
    def get_config_location() -> str:
        """Get config file location"""
        return str(ConfigManager.CONFIG_FILE)
    
    # Check if config file exists
    @staticmethod
    def config_exists() -> bool:
        """Check if config file exists"""
        return ConfigManager.CONFIG_FILE.exists()

# Utility function to create default config file with template
def create_default_config():
    """Create default config file with template"""
    manager = ConfigManager()
    
    template = {
        "api_token": "your-api-token-here",
        "server_host": "localhost",
        "server_port": 50051,
        "timeout": 30,
        "use_ssl": False,
        "log_level": "INFO"
    }
    
    manager.save(template)
    
    print(f"Created default config at: {manager.CONFIG_FILE}")
    print("\nEdit this file to set your API token and server settings.")
    print("\nExample configuration:")
    print(json.dumps(template, indent=2))