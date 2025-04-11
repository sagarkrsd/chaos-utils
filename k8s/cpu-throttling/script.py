#!/usr/bin/env python3

import os
import sys
import time
import argparse
import json
from typing import Dict, List, Optional, Union

try:
    from kubernetes import client, config, stream
except ImportError:
    print("Error: Required package 'kubernetes' is not installed.")
    print("\nTo install required packages, run:")
    print("    pip install -r requirements.txt")
    print("\nOr install directly with:")
    print("    pip install kubernetes")
    sys.exit(1)

# Environment variable names
ENV_NAMESPACE = "NAMESPACE"
ENV_CONTAINER_NAME = "CONTAINER_NAME"
ENV_LABEL_SELECTOR = "LABEL_SELECTOR"
ENV_KUBECONFIG = "KUBECONFIG"
ENV_CGROUP_PATH = "CGROUP_PATH"
ENV_COMPLETE_CGROUP_PATH = "COMPLETE_CGROUP_PATH"
ENV_WAIT_SECONDS = "WAIT_SECONDS"

def debug_print(message: str, verbose: bool = True) -> None:
    """Print debug messages if verbose mode is enabled"""
    if verbose:
        print(message)

def get_kubernetes_client(kubeconfig_path: Optional[str] = None, verbose: bool = False):
    """Initialize and return Kubernetes client with proper configuration"""
    try:
        if kubeconfig_path:
            config.load_kube_config(kubeconfig_path)
            debug_print(f"Using Kubernetes configuration from: {kubeconfig_path}", verbose)
        else:
            try:
                # Try loading in-cluster config first
                config.load_incluster_config()
                debug_print("Using in-cluster Kubernetes configuration", verbose)
            except config.ConfigException:
                # Fall back to default kubeconfig
                config.load_kube_config()
                debug_print(f"Using default Kubernetes configuration from: {os.getenv('KUBECONFIG', '~/.kube/config')}", verbose)
    except Exception as e:
        raise Exception(f"Failed to load Kubernetes configuration: {str(e)}")
    
    return client.CoreV1Api()

def exec_in_container(v1: client.CoreV1Api, 
                     namespace: str, 
                     pod_name: str, 
                     container_name: str, 
                     command: List[str],
                     verbose: bool = False) -> Optional[str]:
    """Execute a command inside a container and return the output"""
    try:
        resp = stream.stream(v1.connect_get_namespaced_pod_exec,
                           pod_name,
                           namespace,
                           container=container_name,
                           command=command,
                           stderr=True,
                           stdin=False,
                           stdout=True,
                           tty=False)
        return resp
    except Exception as e:
        debug_print(f"Error executing command in container: {e}", verbose)
        return None

def get_cpu_stats(v1: client.CoreV1Api,
                  namespace: str,
                  pod_name: str,
                  container_name: str,
                  cgroup_base_path: Optional[str] = None,
                  complete_cgroup_path: Optional[str] = None,
                  verbose: bool = False) -> Optional[Dict]:
    """Get CPU stats from inside the container"""
    try:
        # Construct the command based on provided paths
        if complete_cgroup_path:
            check_cmd = [
                "sh", "-c",
                f"if [ -f {complete_cgroup_path} ]; then "
                f"echo \"Found: {complete_cgroup_path}\"; "
                f"cat {complete_cgroup_path}; exit 0; "
                f"else echo \"Path not found: {complete_cgroup_path}\"; exit 1; fi"
            ]
        elif cgroup_base_path:
            check_cmd = [
                "sh", "-c",
                f"for p in {cgroup_base_path}/cpu.stat {cgroup_base_path}/cpu/cpu.stat; do "
                "if [ -f $p ]; then echo \"Found: $p\"; cat $p; exit 0; fi; "
                "done; echo 'No cpu.stat found in base path'; exit 1"
            ]
        else:
            check_cmd = [
                "sh", "-c",
                "for p in /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/cpu/cpu.stat; do "
                "if [ -f $p ]; then echo \"Found: $p\"; cat $p; exit 0; fi; "
                "done; echo 'No cpu.stat found'; exit 1"
            ]
        
        debug_print(f"Executing command in container: {' '.join(check_cmd)}", verbose)
        output = exec_in_container(v1, namespace, pod_name, container_name, check_cmd, verbose)
        
        if not output or "No cpu.stat found" in output or "Path not found" in output:
            debug_print("No cpu.stat file found in container", verbose)
            if verbose:
                # List available files in cgroup directory
                ls_cmd = ["sh", "-c", "ls -R /sys/fs/cgroup/"]
                ls_output = exec_in_container(v1, namespace, pod_name, container_name, ls_cmd, verbose)
                debug_print("Available files in /sys/fs/cgroup:", verbose)
                debug_print(ls_output, verbose)
            return None

        stats = {
            'nr_periods': 0,
            'nr_throttled': 0,
            'throttled_time': 0
        }

        for line in output.splitlines():
            if "Found:" in line:
                stats['cgroup_path_used'] = line.split("Found:")[1].strip()
                continue
            
            parts = line.strip().split()
            if len(parts) == 2:
                key, value = parts
                if key == 'nr_periods':
                    stats['nr_periods'] = int(value)
                elif key == 'nr_throttled':
                    stats['nr_throttled'] = int(value)
                elif key == 'throttled_time':
                    stats['throttled_time'] = int(value)

        debug_print(f"\nProcessed CPU stats for pod {pod_name}:", verbose)
        debug_print(f"  Nr Periods: {stats['nr_periods']}", verbose)
        debug_print(f"  Nr Throttled: {stats['nr_throttled']}", verbose)
        debug_print(f"  Throttled Time: {stats['throttled_time']} ns", verbose)
        debug_print(f"  Cgroup Path: {stats.get('cgroup_path_used', 'unknown')}", verbose)

        if stats['nr_periods'] == 0:
            debug_print("Warning: No CPU periods recorded", verbose)
            return None

        return stats

    except Exception as e:
        debug_print(f"Error reading CPU stats for pod '{pod_name}': {str(e)}", verbose)
        if verbose:
            import traceback
            debug_print(traceback.format_exc(), verbose)
        return None

def get_throttling_percentage(namespace: Optional[str] = None,
                            container_name: Optional[str] = None,
                            label_selector: Optional[str] = None,
                            kubeconfig_path: Optional[str] = None,
                            cgroup_base_path: Optional[str] = None,
                            complete_cgroup_path: Optional[str] = None,
                            wait_seconds: Optional[float] = None,
                            verbose: bool = False) -> Dict:
    """Calculate CPU throttling percentage for specified containers"""
    try:
        # Get values from parameters or environment variables
        namespace = namespace or os.getenv(ENV_NAMESPACE)
        container_name = container_name or os.getenv(ENV_CONTAINER_NAME)
        label_selector = label_selector or os.getenv(ENV_LABEL_SELECTOR)
        kubeconfig_path = kubeconfig_path or os.getenv(ENV_KUBECONFIG)
        
        # Handle cgroup paths with proper precedence
        if cgroup_base_path is None and complete_cgroup_path is None:
            cgroup_base_path = os.getenv(ENV_CGROUP_PATH)
            complete_cgroup_path = os.getenv(ENV_COMPLETE_CGROUP_PATH)
        
        # Handle wait_seconds from env var
        if wait_seconds is None and os.getenv(ENV_WAIT_SECONDS):
            try:
                wait_seconds = float(os.getenv(ENV_WAIT_SECONDS))
            except (TypeError, ValueError):
                debug_print(f"Warning: Invalid {ENV_WAIT_SECONDS} value. Must be a number.", verbose)
        
        debug_print("\nConfiguration:", verbose)
        debug_print(f"Namespace: {namespace}", verbose)
        debug_print(f"Container Name: {container_name}", verbose)
        debug_print(f"Label Selector: {label_selector}", verbose)
        debug_print(f"Kubeconfig Path: {kubeconfig_path}", verbose)
        debug_print(f"Cgroup Base Path: {cgroup_base_path}", verbose)
        debug_print(f"Complete Cgroup Path: {complete_cgroup_path}", verbose)
        debug_print(f"Wait Seconds: {wait_seconds}", verbose)
        
        if not namespace or not container_name or not label_selector:
            raise ValueError("Missing required parameters: namespace, container_name, and label_selector must be provided")

        # Initialize Kubernetes client
        v1 = get_kubernetes_client(kubeconfig_path, verbose)

        # Get pods matching the label selector
        pods = v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector).items
        
        debug_print(f"\nFound {len(pods)} pods matching label selector", verbose)
        
        if not pods:
            raise ValueError(f"No pods found with label selector '{label_selector}' in namespace '{namespace}'.")

        total_throttling_percentage = 0
        valid_container_count = 0
        pod_results = []

        for pod in pods:
            debug_print(f"\nProcessing pod: {pod.metadata.name}", verbose)
            
            # Get initial measurement
            debug_print(f"Taking initial measurement...", verbose)
            initial_stats = get_cpu_stats(v1, namespace, pod.metadata.name, container_name,
                                        cgroup_base_path, complete_cgroup_path, verbose)
            if initial_stats is None:
                continue

            # If wait_seconds is specified, take a second measurement
            if wait_seconds:
                debug_print(f"\nWaiting {wait_seconds} seconds for second measurement...", verbose)
                time.sleep(wait_seconds)
                final_stats = get_cpu_stats(v1, namespace, pod.metadata.name, container_name,
                                          cgroup_base_path, complete_cgroup_path, verbose)
                if final_stats is None:
                    continue

                # Calculate throttling based on the difference
                periods_delta = final_stats['nr_periods'] - initial_stats['nr_periods']
                throttled_delta = final_stats['nr_throttled'] - initial_stats['nr_throttled']
                throttled_time_delta = final_stats['throttled_time'] - initial_stats['throttled_time']

                if periods_delta > 0:
                    throttling_percentage = (throttled_delta / periods_delta) * 100
                    throttled_rate = throttled_time_delta / (periods_delta * 100_000_000)  # 100ms per period
                else:
                    debug_print(f"Warning: No new CPU periods for pod '{pod.metadata.name}'. Skipping.", verbose)
                    continue

                stats_to_use = final_stats  # Use final stats for raw numbers
            else:
                # Calculate throttling from single measurement
                if initial_stats['nr_periods'] > 0:
                    throttling_percentage = (initial_stats['nr_throttled'] / initial_stats['nr_periods']) * 100
                    throttled_rate = initial_stats['throttled_time'] / (initial_stats['nr_periods'] * 100_000_000)
                else:
                    debug_print(f"Warning: No CPU periods recorded for pod '{pod.metadata.name}'. Skipping.", verbose)
                    continue

                stats_to_use = initial_stats  # Use initial stats for raw numbers

            total_throttling_percentage += throttling_percentage
            valid_container_count += 1
            
            pod_result = {
                "pod_name": pod.metadata.name,
                "throttling_percentage": throttling_percentage,
                "throttled_rate": throttled_rate,
                "nr_periods": stats_to_use['nr_periods'],
                "nr_throttled": stats_to_use['nr_throttled'],
                "throttled_time_ns": stats_to_use['throttled_time'],
                "cgroup_path": stats_to_use.get('cgroup_path_used', 'unknown')
            }

            if wait_seconds:
                pod_result.update({
                    "periods_delta": periods_delta,
                    "throttled_delta": throttled_delta,
                    "throttled_time_delta_ns": throttled_time_delta
                })

            pod_results.append(pod_result)
            debug_print(f"\nPod '{pod.metadata.name}':", verbose)
            debug_print(f"  CPU Throttling: {throttling_percentage:.2f}%", verbose)
            debug_print(f"  Throttled Rate: {throttled_rate:.2f}", verbose)

        if valid_container_count == 0:
            return {
                "error": "No valid containers found for calculating throttling percentage.",
                "pod_results": pod_results
            }

        average_throttling = total_throttling_percentage / valid_container_count
        
        return {
            "average_throttling": average_throttling,
            "measurement_type": "differential" if wait_seconds else "instantaneous",
            "container_name": container_name,
            "pod_results": pod_results,
            "valid_container_count": valid_container_count
        }

    except Exception as e:
        return {"error": str(e)}

def main():
    parser = argparse.ArgumentParser(description='Calculate CPU throttling percentage for Kubernetes containers')
    
    # Required Kubernetes options
    k8s_group = parser.add_argument_group('Kubernetes options')
    k8s_group.add_argument('--namespace',
                          help=f'Kubernetes namespace (overrides {ENV_NAMESPACE} env var)')
    k8s_group.add_argument('--container-name',
                          help=f'Container name to monitor (overrides {ENV_CONTAINER_NAME} env var)')
    k8s_group.add_argument('--label-selector',
                          help=f'Label selector for pods (overrides {ENV_LABEL_SELECTOR} env var)')
    k8s_group.add_argument('--kubeconfig',
                          help=f'Path to kubeconfig file (overrides {ENV_KUBECONFIG} env var)')
    
    # Optional cgroup path options (mutually exclusive)
    path_group = parser.add_mutually_exclusive_group()
    path_group.add_argument('--cgroup-path',
                          help=f'Base path for cgroup filesystem (overrides {ENV_CGROUP_PATH} env var)')
    path_group.add_argument('--complete-cgroup-path',
                          help=f'Complete path to the cgroup cpu.stat file (overrides {ENV_COMPLETE_CGROUP_PATH} env var)')
    
    # Optional measurement options
    parser.add_argument('--wait-seconds', type=float,
                       help=f'Time to wait between measurements in seconds (overrides {ENV_WAIT_SECONDS} env var)')

    # Output options
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Enable verbose output')

    args = parser.parse_args()

    # Clear environment variables if command-line arguments are provided
    if args.cgroup_path or args.complete_cgroup_path:
        os.environ.pop(ENV_CGROUP_PATH, None)
        os.environ.pop(ENV_COMPLETE_CGROUP_PATH, None)

    result = get_throttling_percentage(
        namespace=args.namespace,
        container_name=args.container_name,
        label_selector=args.label_selector,
        kubeconfig_path=args.kubeconfig,
        cgroup_base_path=args.cgroup_path,
        complete_cgroup_path=args.complete_cgroup_path,
        wait_seconds=args.wait_seconds,
        verbose=args.verbose
    )

    # Always output JSON with all values
    if "error" in result:
        error_result = {
            "error": result["error"],
            "status": "error",
            "timestamp": time.time()
        }
        print(json.dumps(error_result, indent=2))
        sys.exit(1)
    else:
        output = {
            "status": "success",
            "timestamp": time.time(),
            "average_throttling_percentage": result["average_throttling"],
            "measurement_type": result["measurement_type"],
            "container_name": result["container_name"],
            "valid_container_count": result["valid_container_count"],
            "pods": []
        }
        
        for pod in result["pod_results"]:
            pod_data = {
                "name": pod["pod_name"],
                "throttling_percentage": pod["throttling_percentage"],
                "throttled_rate": pod["throttled_rate"],
                "periods": pod["nr_periods"],
                "throttled_count": pod["nr_throttled"],
                "throttled_time_ns": pod["throttled_time_ns"],
                "cgroup_path": pod["cgroup_path"]
            }
            
            # Add differential measurement data if available
            if "periods_delta" in pod:
                pod_data.update({
                    "periods_delta": pod["periods_delta"],
                    "throttled_delta": pod["throttled_delta"],
                    "throttled_time_delta_ns": pod["throttled_time_delta_ns"]
                })
                
            output["pods"].append(pod_data)
        
        print(json.dumps(output, indent=2))

if __name__ == "__main__":
    main()
