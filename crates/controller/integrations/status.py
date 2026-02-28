"""Status reporting and health monitoring"""

from typing import Dict, Optional
from datetime import datetime
import platform
import psutil

# Status Reporter class to gather and report system and connection status, as well as session and metrics info
class StatusReporter:
    """Report system and connection status"""
    
    # Static methods to gather system and connection status, and generate comprehensive reports
    @staticmethod
    def get_system_status() -> Dict:
        """Get current system status with detailed CPU, memory, disk, and network breakdown."""
        try:
            cpu_freq = psutil.cpu_freq()
            disk = psutil.disk_usage('/')
            net = psutil.net_io_counters()
            mem = psutil.virtual_memory()
            return {
                'timestamp': datetime.now().isoformat(),
                'platform': platform.system(),
                'platform_release': platform.release(),
                'architecture': platform.machine(),
                'python_version': platform.python_version(),
                # Kept for backward compatibility
                'cpu_percent': psutil.cpu_percent(interval=1),
                'memory_percent': mem.percent,
                'disk_percent': disk.percent,
                # Detailed breakdown
                'cpu': {
                    'logical_cores': psutil.cpu_count(logical=True),
                    'physical_cores': psutil.cpu_count(logical=False),
                    'percent': psutil.cpu_percent(interval=0),
                    'freq_mhz': round(cpu_freq.current, 1) if cpu_freq else None,
                },
                'memory': {
                    'total_gb': round(mem.total / 1024 ** 3, 2),
                    'available_gb': round(mem.available / 1024 ** 3, 2),
                    'used_gb': round(mem.used / 1024 ** 3, 2),
                    'percent': mem.percent,
                    'total_bytes': mem.total,
                    'available_bytes': mem.available,
                },
                'disk': {
                    'total_gb': round(disk.total / 1024 ** 3, 2),
                    'free_gb': round(disk.free / 1024 ** 3, 2),
                    'used_gb': round(disk.used / 1024 ** 3, 2),
                    'percent': disk.percent,
                },
                'network': {
                    'bytes_sent': net.bytes_sent,
                    'bytes_recv': net.bytes_recv,
                    'packets_sent': net.packets_sent,
                    'packets_recv': net.packets_recv,
                    'errin': net.errin,
                    'errout': net.errout,
                },
            }
        except Exception as e:
            return {
                'timestamp': datetime.now().isoformat(),
                'error': str(e),
                'platform': platform.system(),
                'cpu_percent': 0,
                'memory_percent': 0,
                'disk_percent': 0,
            }
    
    # Static method to get connection status from gRPC client
    @staticmethod
    def get_connection_status(grpc_client) -> Dict:
        """Get connection status, preferring rich server-query data when available."""
        # Try the richer QueryServers call first (available without auth)
        try:
            server_status = grpc_client.query_server_status()
            if server_status and server_status.get('servers'):
                srv = server_status['servers'][0]
                conn = srv.get('current_connection')
                stat = srv.get('status', {})
                ident = srv.get('identity', {})
                return {
                    'connected': stat.get('agent_connected', False),
                    'agent_id': conn.get('agent_id') if conn else None,
                    'agent_hostname': conn.get('agent_hostname') if conn else None,
                    'agent_ip': conn.get('agent_ip') if conn else None,
                    'connection_id': conn.get('connection_id') if conn else None,
                    'connection_state': conn.get('state') if conn else None,
                    'commands_executed': conn.get('commands_executed', 0) if conn else 0,
                    'connected_at': conn.get('connected_at') if conn else None,
                    'last_heartbeat': conn.get('last_heartbeat') if conn else None,
                    'server_id': ident.get('server_id'),
                    'server_uptime_seconds': stat.get('uptime_seconds', 0),
                    'server_total_commands': stat.get('total_commands_processed', 0),
                    'timestamp': datetime.now().isoformat(),
                }
        except Exception:
            pass

        # Fallback: legacy get_agent_info (requires auth, returns agent-side info)
        try:
            agent_info = grpc_client.get_agent_info()
            return {
                'connected': agent_info is not None,
                'agent_os': agent_info.get('os_type') if agent_info else None,
                'agent_version': agent_info.get('agent_version') if agent_info else None,
                'timestamp': datetime.now().isoformat(),
            }
        except Exception:
            return {
                'connected': False,
                'timestamp': datetime.now().isoformat(),
                'error': 'Could not query connection status',
            }
    
    # Static method to generate a comprehensive status report combining session, metrics, connection, and system status
    @staticmethod
    def get_session_status(session) -> Dict:
        """Extract enriched session status from a Session object."""
        if not session:
            return {}
        raw = session.to_dict()
        return {
            'host': raw.get('host'),
            'port': raw.get('port'),
            'os_type': raw.get('os_type'),
            'os_version': raw.get('os_version'),
            'started_at': raw.get('started_at'),
            'duration_seconds': raw.get('duration_seconds', 0),
            'idle_seconds': raw.get('idle_seconds', 0),
            'state': raw.get('state'),
            'active': raw.get('active'),
            'vm_shutdown': raw.get('vm_shutdown'),
            'reconnection_attempts': raw.get('reconnection_attempts', 0),
            'health_check_failures': raw.get('health_check_failures', 0),
            'recent_events': raw.get('events', [])[-10:],
        }

    # Static method to extract detailed metrics status from a MetricsCollector object
    @staticmethod
    def get_metrics_status(metrics) -> Dict:
        """Get full metrics breakdown from a MetricsCollector object."""
        if not metrics:
            return {}
        stats = metrics.get_stats()
        return {
            'total_commands': stats.get('total_commands', 0),
            'successful': stats.get('successful', 0),
            'failed': stats.get('failed', 0),
            'success_rate': stats.get('success_rate', 0),
            'avg_execution_time_ms': stats.get('avg_execution_time_ms', 0),
            'min_execution_time_ms': stats.get('min_execution_time_ms', 0),
            'max_execution_time_ms': stats.get('max_execution_time_ms', 0),
            'percentiles': {
                'p50_ms': stats.get('p50_ms', 0),
                'p95_ms': stats.get('p95_ms', 0),
                'p99_ms': stats.get('p99_ms', 0),
            },
            'commands_by_type': stats.get('commands_by_type', {}),
            'errors': {
                'by_type': stats.get('errors_by_type', {}),
                'rate_limit_hits': stats.get('rate_limit_hits', 0),
            },
            'uptime_seconds': stats.get('uptime_seconds', 0),
        }

    # Static method to generate a comprehensive status report combining session, metrics, connection, and system status
    @staticmethod
    def generate_status_report(session, metrics, grpc_client) -> Dict:
        """Generate comprehensive status report"""
        return {
            'timestamp': datetime.now().isoformat(),
            'session': StatusReporter.get_session_status(session),
            'metrics': StatusReporter.get_metrics_status(metrics),
            'connection': StatusReporter.get_connection_status(grpc_client),
            'system': StatusReporter.get_system_status(),
        }

    # Static method to print the status report in a human-readable format
    @staticmethod
    def print_status_report(report: Dict):
        """Print status report in human-readable format"""
        print("\n=== Status Report ===")
        print(f"Generated: {report['timestamp']}")

        session = report.get('session', {})
        if session:
            print("\n[Session]")
            print(f"  Host: {session.get('host')}:{session.get('port')}")
            print(f"  OS: {session.get('os_type')} {session.get('os_version')}")
            print(f"  Duration: {session.get('duration_seconds', 0):.2f}s")
            print(f"  Idle: {session.get('idle_seconds', 0):.2f}s")
            if session.get('vm_shutdown'):
                print("  ⚠ VM/Container shutdown detected")

        conn = report.get('connection', {})
        print("\n[Connection]")
        print(f"  Connected: {conn.get('connected')}")
        if conn.get('agent_hostname'):
            print(f"  Agent Hostname: {conn.get('agent_hostname')}")
        if conn.get('agent_ip'):
            print(f"  Agent IP: {conn.get('agent_ip')}")
        if conn.get('agent_os'):
            print(f"  Agent OS: {conn.get('agent_os')}")
        if conn.get('server_uptime_seconds'):
            print(f"  Server Uptime: {conn.get('server_uptime_seconds')}s")

        metrics = report.get('metrics', {})
        if metrics:
            percs = metrics.get('percentiles', {})
            print("\n[Metrics]")
            print(f"  Commands: {metrics.get('total_commands', 0)}")
            print(f"  Success Rate: {metrics.get('success_rate', 0):.2f}%")
            print(f"  Avg Time: {metrics.get('avg_execution_time_ms', 0):.2f}ms")
            print(f"  p50/p95/p99: {percs.get('p50_ms', 0)}/{percs.get('p95_ms', 0)}/{percs.get('p99_ms', 0)}ms")

        sys_info = report.get('system', {})
        print("\n[System (controller host)]")
        print(f"  CPU: {sys_info.get('cpu_percent', sys_info.get('cpu', {}).get('percent', 0))}%")
        print(f"  Memory: {sys_info.get('memory_percent', sys_info.get('memory', {}).get('percent', 0))}%")
        print(f"  Disk: {sys_info.get('disk_percent', sys_info.get('disk', {}).get('percent', 0))}%")

        print("====================\n")

    # Static methods to print detailed breakdowns of connection, metrics, and system info for specific subcommands
    @staticmethod
    def print_connection_detail(conn: Dict):
        """Print detailed connection info (used by `status connection` subcommand)."""
        print("\n━━━ Connection Detail ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"  Connection ID:    {conn.get('connection_id', 'N/A')}")
        print(f"  Agent ID:         {conn.get('agent_id', 'N/A')}")
        print(f"  Agent Hostname:   {conn.get('agent_hostname', 'N/A')}")
        print(f"  Agent IP:         {conn.get('agent_ip', 'N/A')}")
        state_map = {0: "CONNECTING", 1: "CONNECTED", 2: "ACTIVE",
                     3: "IDLE", 4: "DISCONNECTING", 5: "DISCONNECTED", 6: "ERROR"}
        raw_state = conn.get('connection_state', -1)
        print(f"  State:            {state_map.get(raw_state, str(raw_state))}")
        print(f"  Commands (agent): {conn.get('commands_executed', 0)}")
        if conn.get('server_id'):
            print(f"  Server ID:        {conn.get('server_id')}")
        if conn.get('server_uptime_seconds') is not None:
            print(f"  Server Uptime:    {conn.get('server_uptime_seconds')}s")
        if conn.get('connected_at'):
            try:
                from datetime import datetime as _dt
                ts = _dt.fromtimestamp(conn['connected_at']).strftime('%Y-%m-%d %H:%M:%S')
                print(f"  Connected At:     {ts}")
            except Exception:
                print(f"  Connected At:     {conn.get('connected_at')}")
        if conn.get('last_heartbeat'):
            try:
                from datetime import datetime as _dt
                ts = _dt.fromtimestamp(conn['last_heartbeat']).strftime('%Y-%m-%d %H:%M:%S')
                print(f"  Last Heartbeat:   {ts}")
            except Exception:
                print(f"  Last Heartbeat:   {conn.get('last_heartbeat')}")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Static method to print detailed session info (used by `status session` subcommand)
    @staticmethod
    def print_metrics_detail(metrics: Dict):
        """Print detailed metrics breakdown (used by `status metrics` subcommand)."""
        print("\n━━━ Metrics Detail ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"  Total Commands:   {metrics.get('total_commands', 0)}")
        print(f"  Successful:       {metrics.get('successful', 0)}")
        print(f"  Failed:           {metrics.get('failed', 0)}")
        print(f"  Success Rate:     {metrics.get('success_rate', 0):.2f}%")
        print(f"  Uptime:           {metrics.get('uptime_seconds', 0):.1f}s")
        print()
        print("  Execution Time")
        print(f"    Average:  {metrics.get('avg_execution_time_ms', 0):.2f}ms")
        print(f"    Min:      {metrics.get('min_execution_time_ms', 0)}ms")
        print(f"    Max:      {metrics.get('max_execution_time_ms', 0)}ms")
        percs = metrics.get('percentiles', {})
        print(f"    p50:      {percs.get('p50_ms', 0)}ms")
        print(f"    p95:      {percs.get('p95_ms', 0)}ms")
        print(f"    p99:      {percs.get('p99_ms', 0)}ms")
        by_type = metrics.get('commands_by_type', {})
        if by_type:
            print()
            print("  Commands by Type")
            for cmd_type, count in sorted(by_type.items(), key=lambda x: -x[1]):
                print(f"    {cmd_type:<14} {count}")
        errors = metrics.get('errors', {})
        by_err = errors.get('by_type', {})
        rl = errors.get('rate_limit_hits', 0)
        if by_err or rl:
            print()
            print("  Errors")
            for err_type, count in by_err.items():
                print(f"    {err_type:<22} {count}")
            if rl:
                print(f"    rate_limit_hits        {rl}")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    
    # Static method to print detailed system info (used by `status system` subcommand)
    @staticmethod
    def print_system_detail(sys_info: Dict):
        """Print detailed controller host system info (used by `status system` subcommand)."""
        print("\n━━━ System Detail ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        cpu = sys_info.get('cpu', {})
        mem = sys_info.get('memory', {})
        disk = sys_info.get('disk', {})
        net = sys_info.get('network', {})
        print(f"  CPU:         {sys_info.get('cpu_percent', cpu.get('percent', 0))}%")
        print(f"  CPU Cores:   {cpu.get('logical_cores', 'N/A')} logical")
        print(f"  Memory:      {sys_info.get('memory_percent', mem.get('percent', 0))}%  "
            f"({mem.get('used_gb', 0):.1f} / {mem.get('total_gb', 0):.1f} GB)")
        print(f"  Disk:        {sys_info.get('disk_percent', disk.get('percent', 0))}%  "
            f"({disk.get('used_gb', 0):.1f} / {disk.get('total_gb', 0):.1f} GB)")
        if net:
            print(f"  Net sent:    {net.get('bytes_sent_mb', 0) / 1024 **2 :.1f} MB")
            print(f"  Net recv:    {net.get('bytes_recv_mb', 0) / 1024 **2 :.1f} MB")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")