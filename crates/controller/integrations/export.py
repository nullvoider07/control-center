"""Comprehensive export functionality with diagnostics"""

import json
import csv
import io
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
import platform
import psutil


class Exporter:
    """Export with comprehensive diagnostics"""
    
    def __init__(self, output_dir: str = "./exports"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    # Basic export functions
    def export_metrics_json(
        self,
        metrics: Dict,
        filename: Optional[str] = None,
        *,
        include_percentiles: bool = True,
        include_by_type: bool = True,
        include_errors: bool = True,
        command_type_filter: Optional[str] = None,
    ) -> str:
        """Export metrics to JSON file.

        Args:
            metrics:              Full MetricsCollector.get_stats() dict.
            include_percentiles:  Include p50/p95/p99 breakdown.
            include_by_type:      Include per-command-type counts.
            include_errors:       Include error breakdown.
            command_type_filter:  If set, filter commands_by_type to that key
                                  (mouse | keyboard | press | type | other).
        """
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"metrics_{timestamp}.json"

        filepath = self.output_dir / filename

        data: Dict[str, Any] = {
            'exported_at': datetime.now().isoformat(),
            'total_commands': metrics.get('total_commands', 0),
            'successful': metrics.get('successful', 0),
            'failed': metrics.get('failed', 0),
            'success_rate': metrics.get('success_rate', 0),
            'avg_execution_time_ms': metrics.get('avg_execution_time_ms', 0),
            'min_execution_time_ms': metrics.get('min_execution_time_ms', 0),
            'max_execution_time_ms': metrics.get('max_execution_time_ms', 0),
            'uptime_seconds': metrics.get('uptime_seconds', 0),
        }

        if include_percentiles:
            data['percentiles'] = {
                'p50_ms': metrics.get('p50_ms', 0),
                'p95_ms': metrics.get('p95_ms', 0),
                'p99_ms': metrics.get('p99_ms', 0),
            }

        if include_by_type:
            by_type = metrics.get('commands_by_type', {})
            if command_type_filter:
                by_type = {k: v for k, v in by_type.items() if k == command_type_filter}
            data['commands_by_type'] = by_type

        if include_errors:
            data['errors'] = {
                'by_type': metrics.get('errors_by_type', {}),
                'rate_limit_hits': metrics.get('rate_limit_hits', 0),
            }

        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2, default=str)

        return str(filepath)
    
    # Session info export
    def export_session_info(
        self,
        session_info: Dict,
        filename: Optional[str] = None,
        *,
        include_events: bool = True,
        include_reconnection_history: bool = True,
        max_events: int = 50,
    ) -> str:
        """Export session information to JSON.

        Args:
            session_info:                 Session.to_dict() output.
            include_events:               Include session event log.
            include_reconnection_history: Include reconnection attempt details.
            max_events:                   Truncate event list to latest N.
        """
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"session_{timestamp}.json"

        filepath = self.output_dir / filename

        data: Dict[str, Any] = {
            'exported_at': datetime.now().isoformat(),
            'host': session_info.get('host'),
            'port': session_info.get('port'),
            'user_id': session_info.get('user_id'),
            'os_type': session_info.get('os_type'),
            'os_version': session_info.get('os_version'),
            'started_at': session_info.get('started_at'),
            'duration_seconds': session_info.get('duration_seconds', 0),
            'idle_seconds': session_info.get('idle_seconds', 0),
            'active': session_info.get('active', False),
            'state': session_info.get('state'),
            'vm_shutdown': session_info.get('vm_shutdown', False),
            'health_check_failures': session_info.get('health_check_failures', 0),
        }

        if include_reconnection_history:
            data['reconnection'] = {
                'attempts': session_info.get('reconnection_attempts', 0),
                'max_attempts': session_info.get('max_reconnection_attempts', 5),
            }

        if include_events:
            events = session_info.get('events', [])
            data['events'] = events[-max_events:] if len(events) > max_events else events

        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2, default=str)

        return str(filepath)
    
    # Command log export
    def export_command_log(
        self,
        commands: List[Dict],
        filename: Optional[str] = None,
        *,
        fmt: str = 'csv',
        command_type_filter: Optional[str] = None,
        success_only: bool = False,
        failed_only: bool = False,
        last_n: Optional[int] = None,
    ) -> str:
        """Export command execution log with optional filtering.

        Args:
            commands:             List of command metric dicts.
            fmt:                  Output format: 'csv' | 'json' | 'ndjson'.
            command_type_filter:  Only include commands of this type.
            success_only:         Only include successful commands.
            failed_only:          Only include failed commands.
            last_n:               Only include the last N commands.
        """
        # Apply filters
        filtered = list(commands)

        if command_type_filter:
            filtered = [c for c in filtered if c.get('command_type') == command_type_filter]
        if success_only:
            filtered = [c for c in filtered if c.get('success')]
        elif failed_only:
            filtered = [c for c in filtered if not c.get('success')]
        if last_n is not None:
            filtered = filtered[-last_n:]

        ext = {'csv': 'csv', 'json': 'json', 'ndjson': 'ndjson'}.get(fmt, 'csv')
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"commands_{timestamp}.{ext}"

        filepath = self.output_dir / filename

        if fmt == 'json':
            with open(filepath, 'w') as f:
                json.dump(
                    {'exported_at': datetime.now().isoformat(),
                     'total': len(filtered), 'commands': filtered},
                    f, indent=2, default=str,
                )
        elif fmt == 'ndjson':
            with open(filepath, 'w') as f:
                for row in filtered:
                    f.write(json.dumps(row, default=str) + '\n')
        else:
            # csv (default)
            if not filtered:
                filepath.write_text("")
                return str(filepath)
            with open(filepath, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=filtered[0].keys())
                writer.writeheader()
                writer.writerows(filtered)

        return str(filepath)

    def export_command_log_csv(self, commands: List[Dict], filename: Optional[str] = None) -> str:
        """Export command log to CSV. Backward-compatible alias for export_command_log."""
        return self.export_command_log(commands, filename, fmt='csv')
    
    # Comprehensive export with diagnostics
    def export_full_diagnostics(
        self,
        session_info: Dict,
        metrics: Dict,
        errors: Optional[List[Dict]] = None,
        *,
        include_system: bool = True,
        include_html: bool = True,
    ) -> Dict[str, str]:
        """Export comprehensive diagnostics report.

        Args:
            include_system: Gather and export local controller system info.
            include_html:   Generate an HTML summary report.

        Returns:
            Dict mapping label → filepath for each generated file.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        files = {}

        # 1. Session info
        files['session'] = self.export_session_info(
            session_info, f"session_{timestamp}.json",
            include_events=True, include_reconnection_history=True,
        )

        # 2. Metrics
        files['metrics'] = self.export_metrics_json(
            metrics, f"metrics_{timestamp}.json",
            include_percentiles=True, include_by_type=True, include_errors=True,
        )

        # 3. Errors (if any)
        if errors:
            files['errors'] = self.export_command_log(
                errors, f"errors_{timestamp}.csv", fmt='csv', failed_only=True,
            )

        # 4. System info (local controller host)
        if include_system:
            system_info = self._gather_system_info()
            system_file = self.output_dir / f"system_{timestamp}.json"
            with open(system_file, 'w') as f:
                json.dump(system_info, f, indent=2)
            files['system'] = str(system_file)
        else:
            system_info = {}

        # 5. HTML report
        if include_html:
            html_file = self.output_dir / f"report_{timestamp}.html"
            self._generate_html_report(html_file, session_info, metrics, errors or [], system_info)
            files['html_report'] = str(html_file)

        return files
    
    def export_audit_logs(
        self,
        log_dir: str = "./logs/audit",
        filename: Optional[str] = None,
        *,
        fmt: str = 'json',
        since: Optional[str] = None,
        event_type_filter: Optional[str] = None,
        level_filter: Optional[str] = None,
        last_n: Optional[int] = None,
    ) -> str:
        """Export audit log records from the daily-rotating audit log files.

        Args:
            log_dir:             Directory containing AuditLogger output files.
            fmt:                 Output format: 'json' | 'csv' | 'ndjson'.
            since:               ISO date string (YYYY-MM-DD). Exclude entries before this.
            event_type_filter:   Filter by event type keyword:
                                 auth | session | vm_shutdown | reconnect | permission.
            level_filter:        Filter by log level: INFO | WARNING | ERROR.
            last_n:              Only include the last N matching records.
        """
        log_path = Path(log_dir)
        records: List[Dict] = []

        log_files = sorted(log_path.glob("audit*")) if log_path.exists() else []

        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since)
            except ValueError:
                pass

        event_keywords = {
            'auth': 'auth_attempt',
            'session': 'session_',
            'vm_shutdown': 'vm_shutdown',
            'reconnect': 'reconnection',
            'permission': 'permission_denied',
        }

        for log_file in log_files:
            try:
                with open(log_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # Date filter
                        if since_dt and record.get('timestamp'):
                            try:
                                rec_dt = datetime.fromisoformat(record['timestamp'])
                                if rec_dt < since_dt:
                                    continue
                            except (ValueError, TypeError):
                                pass

                        # Level filter
                        if level_filter:
                            if record.get('level', '').upper() != level_filter.upper():
                                continue

                        # Event type filter
                        if event_type_filter:
                            keyword = event_keywords.get(event_type_filter.lower(), event_type_filter)
                            if (keyword not in str(record.get('event', '')) and
                                    keyword not in str(record.get('message', ''))):
                                continue

                        records.append(record)
            except (IOError, OSError):
                continue

        if last_n is not None:
            records = records[-last_n:]

        ext = {'json': 'json', 'csv': 'csv', 'ndjson': 'ndjson'}.get(fmt, 'json')
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"audit_{timestamp}.{ext}"

        filepath = self.output_dir / filename

        if fmt == 'json':
            with open(filepath, 'w') as f:
                json.dump(
                    {'exported_at': datetime.now().isoformat(),
                     'total': len(records), 'records': records},
                    f, indent=2, default=str,
                )
        elif fmt == 'ndjson':
            with open(filepath, 'w') as f:
                for row in records:
                    f.write(json.dumps(row, default=str) + '\n')
        else:
            # csv — flatten nested dicts to top-level fields
            flat: List[Dict] = []
            for r in records:
                flat.append({
                    'timestamp': r.get('timestamp', ''),
                    'level': r.get('level', ''),
                    'event': r.get('event', ''),
                    'message': r.get('message', ''),
                    'user_id': r.get('user_id', ''),
                    'session_id': r.get('session_id', ''),
                    'ip_address': r.get('ip_address', ''),
                    'success': r.get('success', ''),
                    'duration_seconds': r.get('duration_seconds', ''),
                    'connection_id': r.get('connection_id', ''),
                    'reason': r.get('reason', ''),
                })
            if flat:
                with open(filepath, 'w', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=flat[0].keys())
                    writer.writeheader()
                    writer.writerows(flat)
            else:
                filepath.write_text("")

        return str(filepath)

    # Helper functions
    def _gather_system_info(self) -> Dict:
        """Gather system information"""
        try:
            return {
                'timestamp': datetime.now().isoformat(),
                'platform': platform.system(),
                'platform_release': platform.release(),
                'platform_version': platform.version(),
                'architecture': platform.machine(),
                'processor': platform.processor(),
                'python_version': platform.python_version(),
                'cpu_count': psutil.cpu_count(),
                'cpu_percent': psutil.cpu_percent(interval=1),
                'memory_total': psutil.virtual_memory().total,
                'memory_available': psutil.virtual_memory().available,
                'memory_percent': psutil.virtual_memory().percent,
            }
        except Exception as e:
            return {
                'timestamp': datetime.now().isoformat(),
                'error': str(e),
                'platform': platform.system(),
                'python_version': platform.python_version(),
            }
    
    # Generate HTML report
    def _generate_html_report(self, filepath, session, metrics, errors, system):
        """Generate HTML diagnostic report"""
        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Control Center Diagnostics Report</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 30px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        h1 {{ color: #333; border-bottom: 3px solid #4CAF50; padding-bottom: 10px; }}
        h2 {{ color: #555; margin-top: 30px; }}
        table {{ border-collapse: collapse; width: 100%; margin: 15px 0; }}
        th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
        th {{ background-color: #4CAF50; color: white; }}
        .error {{ color: #f44336; font-weight: bold; }}
        .success {{ color: #4CAF50; font-weight: bold; }}
        .warning {{ color: #ff9800; }}
        .metric-card {{ display: inline-block; background: #f9f9f9; padding: 15px; margin: 10px; border-radius: 5px; min-width: 200px; }}
        .metric-value {{ font-size: 24px; font-weight: bold; color: #4CAF50; }}
        .metric-label {{ color: #666; font-size: 14px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Control Center Diagnostics Report</h1>
        <p><strong>Generated:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        
        <h2>Session Information</h2>
        <table>
            <tr><th>Field</th><th>Value</th></tr>
            <tr><td>Host</td><td>{session.get('host', 'N/A')}</td></tr>
            <tr><td>User ID</td><td>{session.get('user_id', 'N/A')}</td></tr>
            <tr><td>OS</td><td>{session.get('os_type', 'N/A')} {session.get('os_version', '')}</td></tr>
            <tr><td>Duration</td><td>{session.get('duration_seconds', 0):.2f}s</td></tr>
            <tr><td>State</td><td class="{'error' if session.get('state') == 'vm_shutdown' else 'success'}">{session.get('state', 'unknown')}</td></tr>
            <tr><td>VM Shutdown</td><td class="{'error' if session.get('vm_shutdown') else 'success'}">{str(session.get('vm_shutdown', False))}</td></tr>
        </table>
        
        <h2>Performance Metrics</h2>
        <div>
            <div class="metric-card">
                <div class="metric-label">Total Commands</div>
                <div class="metric-value">{metrics.get('total_commands', 0)}</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Success Rate</div>
                <div class="metric-value" style="color: {'#4CAF50' if metrics.get('success_rate', 0) > 90 else '#f44336'}">{metrics.get('success_rate', 0):.1f}%</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Avg Time</div>
                <div class="metric-value">{metrics.get('avg_execution_time_ms', 0):.2f}ms</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">p95 Time</div>
                <div class="metric-value">{metrics.get('p95_ms', 0)}ms</div>
            </div>
        </div>
        
        <h2>Errors ({len(errors)})</h2>
        <table>
            <tr><th>Type</th><th>Message</th><th>Timestamp</th></tr>
            {''.join(f'<tr><td class="error">{e.get("type", "Unknown")}</td><td>{e.get("message", "")}</td><td>{e.get("timestamp", "")}</td></tr>' for e in errors[:20])}
        </table>
        
        <h2>System Information</h2>
        <table>
            <tr><th>Property</th><th>Value</th></tr>
            <tr><td>Platform</td><td>{system.get('platform', 'Unknown')}</td></tr>
            <tr><td>CPU Usage</td><td>{system.get('cpu_percent', 0)}%</td></tr>
            <tr><td>Memory Usage</td><td>{system.get('memory_percent', 0)}%</td></tr>
        </table>
    </div>
</body>
</html>"""
        
        with open(filepath, 'w') as f:
            f.write(html)
    
    # Full report export
    def export_full_report(
        self,
        session_info: Dict,
        metrics: Dict,
        commands: Optional[List[Dict]] = None,
        *,
        command_fmt: str = 'csv',
    ) -> Dict[str, str]:
        """Export complete session report (session + metrics + optional commands).

        Args:
            command_fmt: Format for command log export: 'csv' | 'json' | 'ndjson'.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        files = {
            'session': self.export_session_info(session_info, f"session_{timestamp}.json"),
            'metrics': self.export_metrics_json(metrics, f"metrics_{timestamp}.json"),
        }

        if commands:
            files['commands'] = self.export_command_log(
                commands, f"commands_{timestamp}.{command_fmt}", fmt=command_fmt,
            )

        return files