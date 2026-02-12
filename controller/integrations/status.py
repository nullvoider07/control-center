"""Status reporting and health monitoring"""

from typing import Dict, Optional
from datetime import datetime
import psutil


class StatusReporter:
    """Report system and connection status"""
    
    # Static methods to gather system and connection status, and generate comprehensive reports
    @staticmethod
    def get_system_status() -> Dict:
        """Get current system status"""
        return {
            'timestamp': datetime.now().isoformat(),
            'cpu_percent': psutil.cpu_percent(interval=1),
            'memory_percent': psutil.virtual_memory().percent,
            'disk_percent': psutil.disk_usage('/').percent,
        }
    
    # Static method to get connection status from gRPC client
    @staticmethod
    def get_connection_status(grpc_client) -> Dict:
        """Get connection status"""
        agent_info = grpc_client.get_agent_info()
        
        return {
            'connected': agent_info is not None,
            'agent_os': agent_info.get('os_type') if agent_info else None,
            'agent_version': agent_info.get('agent_version') if agent_info else None,
            'timestamp': datetime.now().isoformat(),
        }
    
    # Static method to generate a comprehensive status report combining session, metrics, connection, and system status
    @staticmethod
    def generate_status_report(session, metrics, grpc_client) -> Dict:
        """Generate comprehensive status report"""
        report = {
            'timestamp': datetime.now().isoformat(),
            'session': session.to_dict() if session else None,
            'metrics': metrics.get_stats(),
            'connection': StatusReporter.get_connection_status(grpc_client),
            'system': StatusReporter.get_system_status(),
        }
        
        return report
    
    # Static method to print the status report in a human-readable format
    @staticmethod
    def print_status_report(report: Dict):
        """Print status report in human-readable format"""
        print("\n=== Status Report ===")
        print(f"Generated: {report['timestamp']}")
        
        if report['session']:
            print("\n[Session]")
            print(f"  Host: {report['session']['host']}:{report['session']['port']}")
            print(f"  OS: {report['session']['os_type']} {report['session']['os_version']}")
            print(f"  Duration: {report['session']['duration_seconds']:.2f}s")
            print(f"  Idle: {report['session']['idle_seconds']:.2f}s")
        
        print("\n[Connection]")
        print(f"  Connected: {report['connection']['connected']}")
        if report['connection']['agent_os']:
            print(f"  Agent OS: {report['connection']['agent_os']}")
        
        print("\n[Metrics]")
        print(f"  Commands: {report['metrics']['total_commands']}")
        print(f"  Success Rate: {report['metrics']['success_rate']:.2f}%")
        print(f"  Avg Time: {report['metrics']['avg_execution_time_ms']:.2f}ms")
        
        print("\n[System]")
        print(f"  CPU: {report['system']['cpu_percent']}%")
        print(f"  Memory: {report['system']['memory_percent']}%")
        print(f"  Disk: {report['system']['disk_percent']}%")
        
        print("====================\n")