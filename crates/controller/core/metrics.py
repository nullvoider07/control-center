"""Metrics collection with comprehensive tracking and analysis"""

import time
import statistics
from typing import Dict, List, Optional, Tuple
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
import logging

# Set up logging
logger = logging.getLogger(__name__)

# Define a dataclass for individual command metrics
@dataclass
class CommandMetric:
    """Individual command execution metric"""
    command: str
    command_type: str
    success: bool
    execution_time_ms: int
    timestamp: float
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    request_id: Optional[str] = None

# Define a dataclass for aggregated session metrics
class MetricsCollector:
    """Metrics collector with comprehensive tracking"""
    
    def __init__(self, history_size: int = 1000):
        # Basic counters
        self.command_count = 0
        self.success_count = 0
        self.failure_count = 0
        self.total_execution_time_ms = 0
        
        # Detailed tracking
        self.execution_times: List[int] = []
        self.commands_by_type = defaultdict(int)
        self.errors_by_type = defaultdict(int)
        
        # Command history (rolling window)
        self.command_history: deque = deque(maxlen=history_size)
        
        # Error tracking
        self.rate_limit_hits = 0
        self.auth_errors = 0
        self.connection_errors = 0
        self.timeout_errors = 0
        
        # Timestamps
        self.start_time = time.time()
        self.last_command_time = 0
        
        # Performance baseline
        self.baseline_established = False
        self.baseline_avg_time_ms = 0
        self.baseline_std_dev = 0
    
    # Method to record command execution
    def record_command(self, command: str, success: bool, execution_time_ms: int, 
                      error_type: Optional[str] = None, error_message: Optional[str] = None):
        """Record command execution"""
        self.command_count += 1
        if success:
            self.success_count += 1
        else:
            self.failure_count += 1
            if error_type:
                self.errors_by_type[error_type] += 1
        
        self.total_execution_time_ms += execution_time_ms
        self.execution_times.append(execution_time_ms)
        
        command_type = self._classify_command(command)
        self.commands_by_type[command_type] += 1
        
        metric = CommandMetric(
            command=command[:100],
            command_type=command_type,
            success=success,
            execution_time_ms=execution_time_ms,
            timestamp=time.time(),
            error_type=error_type,
            error_message=error_message
        )
        self.command_history.append(metric)
        self.last_command_time = time.time()
    
    # Methods to record specific error types
    def record_rate_limit_error(self):
        self.rate_limit_hits += 1
    
    # Method to record authentication errors
    def record_auth_error(self):
        self.auth_errors += 1
    
    # Method to record connection errors
    def record_connection_error(self):
        self.connection_errors += 1
    
    # Method to record timeout errors
    def _classify_command(self, command: str) -> str:
        cmd_lower = command.lower().strip()
        if any(word in cmd_lower for word in ['left', 'right', 'middle', 'double']):
            return 'mouse'
        if cmd_lower.startswith('type '):
            return 'type'
        if cmd_lower.startswith('press '):
            return 'press'
        return 'other'
    
    # Method to compute percentiles
    def get_percentiles(self) -> Dict[str, float]:
        if not self.execution_times:
            return {'p50': 0, 'p75': 0, 'p95': 0, 'p99': 0}
        sorted_times = sorted(self.execution_times)
        n = len(sorted_times)
        return {
            'p50': sorted_times[int(n * 0.50)] if n > 0 else 0,
            'p75': sorted_times[int(n * 0.75)] if n > 0 else 0,
            'p95': sorted_times[int(n * 0.95)] if n > 0 else 0,
            'p99': sorted_times[int(n * 0.99)] if n > 0 else 0,
        }
    
    # Method to get overall statistics
    def get_stats(self) -> Dict:
        avg_time = (self.total_execution_time_ms / self.command_count if self.command_count > 0 else 0)
        success_rate = (self.success_count / self.command_count * 100 if self.command_count > 0 else 0)
        uptime = time.time() - self.start_time
        percentiles = self.get_percentiles()
        
        return {
            'total_commands': self.command_count,
            'successful': self.success_count,
            'failed': self.failure_count,
            'success_rate': success_rate,
            'avg_execution_time_ms': avg_time,
            'min_execution_time_ms': min(self.execution_times) if self.execution_times else 0,
            'max_execution_time_ms': max(self.execution_times) if self.execution_times else 0,
            'p50_ms': percentiles['p50'],
            'p95_ms': percentiles['p95'],
            'p99_ms': percentiles['p99'],
            'uptime_seconds': uptime,
            'commands_by_type': dict(self.commands_by_type),
            'errors_by_type': dict(self.errors_by_type),
            'rate_limit_hits': self.rate_limit_hits,
        }
    
    # Method to print statistics
    def print_stats(self):
        stats = self.get_stats()
        print("\n=== Session Statistics ===")
        print(f"Total Commands: {stats['total_commands']}")
        print(f"Successful: {stats['successful']}")
        print(f"Failed: {stats['failed']}")
        print(f"Success Rate: {stats['success_rate']:.2f}%")
        print(f"Avg Time: {stats['avg_execution_time_ms']:.2f}ms")
        print(f"p50/p95/p99: {stats['p50_ms']}/{stats['p95_ms']}/{stats['p99_ms']}ms")
        print("========================\n")