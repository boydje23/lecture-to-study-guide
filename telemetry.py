"""Application Insights setup. Import and call init_telemetry() FIRST in app.py,
before Flask or any Azure SDK is imported, so auto-instrumentation attaches."""
import os


def init_telemetry():
    connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not connection_string:
        print("APPLICATIONINSIGHTS_CONNECTION_STRING not set; telemetry disabled.")
        return False
    from azure.monitor.opentelemetry import configure_azure_monitor
    configure_azure_monitor(connection_string=connection_string)
    print("Application Insights initialized.")
    return True