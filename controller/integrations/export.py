"""Comprehensive export functionality with diagnostics"""

import json
import csv
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime
import platform
import psutil


class Exporter:
    """Export with comprehensive diagnostics"""
    
    def __init__(self, output_dir: str = "./exports"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    # Basic export functions
    def export_metrics_json(self, metrics: Dict, filename: Optional[str] = None) -> str:
        """Export metrics to JSON file"""
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"metrics_{timestamp}.json"
        
        filepath = self.output_dir / filename
        
        with open(filepath, 'w') as f:
            json.dump(metrics, f, indent=2)
        
        return str(filepath)
    
    # Session info export
    def export_session_info(self, session_info: Dict, filename: Optional[str] = None) -> str:
        """Export session information to JSON"""
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"session_{timestamp}.json"
        
        filepath = self.output_dir / filename
        
        with open(filepath, 'w') as f:
            json.dump(session_info, f, indent=2)
        
        return str(filepath)
    
    # Command log export
    def export_command_log_csv(self, commands: List[Dict], filename: Optional[str] = None) -> str:
        """Export command log to CSV"""
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"commands_{timestamp}.csv"
        
        filepath = self.output_dir / filename
        
        if not commands:
            return str(filepath)
        
        fieldnames = commands[0].keys()
        
        with open(filepath, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(commands)
        
        return str(filepath)
    
    # Comprehensive export with diagnostics
    def export_full_diagnostics(
        self,
        session_info: Dict,
        metrics: Dict,
        errors: Optional[List[Dict]] = None
    ) -> Dict[str, str]:
        """
        Export comprehensive diagnostics report
        
        Returns:
            Dict with paths to generated files
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        files = {}
        
        # 1. Session info
        files['session'] = self.export_session_info(session_info, f"session_{timestamp}.json")
        
        # 2. Metrics
        files['metrics'] = self.export_metrics_json(metrics, f"metrics_{timestamp}.json")
        
        # 3. Errors (if any)
        if errors:
            errors_file = self.output_dir / f"errors_{timestamp}.csv"
            with open(errors_file, 'w', newline='') as f:
                if errors and len(errors) > 0:
                    writer = csv.DictWriter(f, fieldnames=errors[0].keys())
                    writer.writeheader()
                    writer.writerows(errors)
            files['errors'] = str(errors_file)
        
        # 4. System info
        system_info = self._gather_system_info()
        system_file = self.output_dir / f"system_{timestamp}.json"
        with open(system_file, 'w') as f:
            json.dump(system_info, f, indent=2)
        files['system'] = str(system_file)
        
        # 5. HTML report
        html_file = self.output_dir / f"report_{timestamp}.html"
        self._generate_html_report(html_file, session_info, metrics, errors or [], system_info)
        files['html_report'] = str(html_file)
        
        return files
    
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
        commands: Optional[List[Dict]] = None
    ) -> Dict[str, str]:
        """Export complete session report"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        files = {
            'session': self.export_session_info(session_info, f"session_{timestamp}.json"),
            'metrics': self.export_metrics_json(metrics, f"metrics_{timestamp}.json"),
        }
        
        if commands:
            files['commands'] = self.export_command_log_csv(commands, f"commands_{timestamp}.csv")
        
        return files