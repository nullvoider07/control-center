"""Configuration manager with OS detection and controller routing"""

import logging
from typing import Optional, Dict
from enum import Enum

from ..integrations.gRPC import GRPCClient
from ..os_specific.windows_actuation import WindowsActuation
from ..os_specific.macos_actuation import MacOSActuation
from ..os_specific.linux_actuation import LinuxActuation

logger = logging.getLogger(__name__)

# Define OS types
class OSType(Enum):
    """Operating system types"""
    WINDOWS = "WINDOWS"
    MACOS = "MACOS"
    LINUX = "LINUX"
    UNKNOWN = "UNKNOWN"

# Configuration-related exceptions
class ConfigurationError(Exception):
    """Configuration-related errors"""
    pass

# Main configuration manager class
class ConfigManager:
    """
    Configuration manager
    
    Responsibilities:
    - Detect remote OS type from agent
    - Initialize appropriate actuation controller
    - Manage OS-specific settings
    - Validate capabilities
    """
    
    def __init__(self, grpc_client: GRPCClient):
        """
        Initialize configuration manager
        
        Args:
            grpc_client: Connected gRPC client instance
        """
        self.grpc_client = grpc_client
        self.os_type: Optional[OSType] = None
        self.os_version: Optional[str] = None
        self.capabilities: list = []
        self.controller = None
        
        logger.debug("Configuration manager initialized")
    
    # Main initialization method
    def initialize(self) -> bool:
        """
        Initialize configuration by detecting OS and loading controller
        
        Returns:
            True if initialization successful
            
        Raises:
            ConfigurationError: If initialization fails
        """
        try:
            # Get agent information
            agent_info = self.grpc_client.get_agent_info()
            
            if not agent_info:
                raise ConfigurationError("Failed to retrieve agent information")
            
            # Parse OS type
            os_type_str = agent_info.get('os_type', 'UNKNOWN')
            try:
                self.os_type = OSType(os_type_str)
            except ValueError:
                logger.error(f"Unknown OS type: {os_type_str}")
                self.os_type = OSType.UNKNOWN
                raise ConfigurationError(f"Unsupported OS type: {os_type_str}")
            
            self.os_version = agent_info.get('os_version', 'Unknown')
            self.capabilities = agent_info.get('capabilities', [])
            
            logger.info(f"Detected OS: {self.os_type.value} {self.os_version}")
            logger.info(f"Capabilities: {', '.join(self.capabilities)}")
            
            # Validate required capabilities
            if not self._validate_capabilities():
                raise ConfigurationError("Required capabilities not available")
            
            # Initialize appropriate controller
            if not self._initialize_controller():
                raise ConfigurationError("Failed to initialize actuation controller")
            
            logger.info("Configuration initialized successfully")
            return True
            
        except Exception as e:
            logger.error(f"Configuration initialization failed: {e}", exc_info=True)
            raise ConfigurationError(f"Initialization failed: {e}")
    
    # Capability validation method
    def _validate_capabilities(self) -> bool:
        """
        Validate that agent has required capabilities
        
        Returns:
            True if all required capabilities present
        """
        # All agents must support basic mouse and keyboard
        required = {'mouse', 'keyboard'}
        available = set(self.capabilities)
        
        if not required.issubset(available):
            missing = required - available
            logger.error(f"Missing required capabilities: {missing}")
            return False
        
        # OS-specific capability checks
        if self.os_type == OSType.MACOS:
            if 'cliclick' not in available:
                logger.warning(
                    "cliclick not available on macOS agent. "
                    "Install with: brew install cliclick"
                )
                return False
        
        elif self.os_type == OSType.LINUX:
            if 'xdotool' not in available:
                logger.warning(
                    "xdotool not available on Linux agent. "
                    "Install with: sudo apt-get install xdotool"
                )
                return False
        
        logger.debug("All required capabilities available")
        return True
    
    # Controller initialization method
    def _initialize_controller(self) -> bool:
        """
        Initialize OS-specific actuation controller
        
        Returns:
            True if controller initialized successfully
        """
        try:
            if self.os_type == OSType.WINDOWS:
                logger.info("Initializing Windows actuation controller")
                self.controller = WindowsActuation(self.grpc_client)
                
            elif self.os_type == OSType.MACOS:
                logger.info("Initializing macOS actuation controller")
                self.controller = MacOSActuation(self.grpc_client)
                
            elif self.os_type == OSType.LINUX:
                logger.info("Initializing Linux actuation controller")
                self.controller = LinuxActuation(self.grpc_client)
                
            else:
                logger.error(f"Unsupported OS type: {self.os_type}")
                return False
            
            logger.debug("Controller initialized successfully")
            return True
            
        except Exception as e:
            logger.error(f"Controller initialization failed: {e}", exc_info=True)
            return False
    
    # Method to get the initialized controller
    def get_controller(self):
        """
        Get the actuation controller
        
        Returns:
            OS-specific actuation controller instance
            
        Raises:
            ConfigurationError: If controller not initialized
        """
        if not self.controller:
            raise ConfigurationError("Controller not initialized. Call initialize() first.")
        
        return self.controller
    
    # Method to get OS information
    def get_os_info(self) -> Dict:
        """
        Get operating system information
        
        Returns:
            Dictionary with OS details
        """
        return {
            'type': self.os_type.value if self.os_type else 'UNKNOWN',
            'version': self.os_version or 'Unknown',
            'capabilities': self.capabilities,
        }
    
    # Method to check if a specific capability is supported
    def supports_capability(self, capability: str) -> bool:
        """
        Check if a capability is supported
        
        Args:
            capability: Capability name to check
            
        Returns:
            True if capability is supported
        """
        return capability in self.capabilities
    
    # Method to get recommended settings based on OS
    def get_recommended_settings(self) -> Dict:
        """
        Get recommended settings based on OS
        
        Returns:
            Dictionary with recommended settings
        """
        settings = {
            'command_delay_ms': 100,
            'batch_size': 100,
            'timeout_seconds': 30,
        }
        
        # OS-specific adjustments
        if self.os_type == OSType.WINDOWS:
            settings['command_delay_ms'] = 100
            settings['supports_here'] = True
            settings['supports_drag'] = True
            
        elif self.os_type == OSType.MACOS:
            settings['command_delay_ms'] = 50
            settings['supports_here'] = True
            settings['supports_drag'] = True
            settings['supports_scroll'] = True
            
        elif self.os_type == OSType.LINUX:
            settings['command_delay_ms'] = 50
            settings['supports_here'] = True
            settings['supports_drag'] = True
            settings['requires_display'] = True
        
        return settings
    
    # Method to validate if a command is compatible with the current OS
    def validate_command_compatibility(self, command: str) -> tuple[bool, str]:
        """
        Validate if command is compatible with current OS
        
        Args:
            command: Command to validate
            
        Returns:
            Tuple of (is_valid, message)
        """
        # Check for OS-specific syntax
        if self.os_type == OSType.WINDOWS:
            # Windows accepts simple coordinate format
            if command.startswith('cliclick') or command.startswith('xdotool'):
                return False, f"Command uses {command.split()[0]} which is not for Windows"
        
        elif self.os_type == OSType.MACOS:
            pass
        
        elif self.os_type == OSType.LINUX:
            pass
        
        return True, ""
    
    # Method to reload configuration (useful after agent updates)
    def reload(self) -> bool:
        """
        Reload configuration (useful after agent updates)
        
        Returns:
            True if reload successful
        """
        logger.info("Reloading configuration...")
        
        try:
            return self.initialize()
        except Exception as e:
            logger.error(f"Configuration reload failed: {e}")
            return False

# Additional configuration utilities
class EnvironmentConfig:
    """Environment-based configuration"""
    
    # Static method to load configuration from environment variables
    @staticmethod
    def from_environment() -> Dict:
        """
        Load configuration from environment variables
        
        Returns:
            Configuration dictionary
        """
        import os
        
        config = {
            'server_host': os.getenv('CONTROL_CENTER_HOST', 'localhost'),
            'server_port': int(os.getenv('CONTROL_CENTER_PORT', '50051')),
            'use_ssl': os.getenv('CONTROL_CENTER_SSL', 'false').lower() == 'true',
            'timeout': int(os.getenv('CONTROL_CENTER_TIMEOUT', '30')),
            'log_level': os.getenv('CONTROL_CENTER_LOG_LEVEL', 'INFO'),
            'log_dir': os.getenv('CONTROL_CENTER_LOG_DIR', './logs'),
        }
        
        logger.debug(f"Loaded configuration from environment: {config}")
        return config
    
    # Static method to load configuration from a JSON file
    @staticmethod
    def from_file(filepath: str) -> Dict:
        """
        Load configuration from JSON file
        
        Args:
            filepath: Path to configuration file
            
        Returns:
            Configuration dictionary
        """
        import json
        from pathlib import Path
        
        config_path = Path(filepath)
        
        if not config_path.exists():
            raise ConfigurationError(f"Configuration file not found: {filepath}")
        
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
            
            logger.debug(f"Loaded configuration from file: {filepath}")
            return config
            
        except json.JSONDecodeError as e:
            raise ConfigurationError(f"Invalid JSON in configuration file: {e}")
        except Exception as e:
            raise ConfigurationError(f"Failed to read configuration file: {e}")