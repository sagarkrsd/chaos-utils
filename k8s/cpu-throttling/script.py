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
            # 'throttled_time': 0
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
                # elif key == 'throttled_time':
                #     stats['throttled_time'] = int(value)

        debug_print(f"\nProcessed CPU stats for pod {pod_name}:", verbose)
        debug_print(f"  Nr Periods: {stats['nr_periods']}", verbose)
        debug_print(f"  Nr Throttled: {stats['nr_throttled']}", verbose)
        # debug_print(f"  Throttled Time: {stats['throttled_time']} ns", verbose)
        debug_print(f"  Cgroup Path: {stats.get('cgroup_path_used', 'unknown')}", verbose)

        if stats['nr_periods'] == 0:
            debug_print(f"Warning: No CPU periods recorded for pod {pod_name}", verbose)
            stats['nr_throttled'] = 0  # Ensure throttled count is 0 when no periods
            return stats

        return stats

    except Exception as e:
        debug_print(f"Error reading CPU stats for pod '{pod_name}': {str(e)}", verbose)
        if verbose:
            import traceback
            debug_print(traceback.format_exc(), verbose)
        return None

def get_container_cpu_stats(pod_name: str, container_name: str, namespace: str,
                            cgroup_base_path: Optional[str] = None,
                            complete_cgroup_path: Optional[str] = None,
                            verbose: bool = False) -> Optional[Dict]:
    v1 = get_kubernetes_client(verbose=verbose)
    return get_cpu_stats(v1, namespace, pod_name, container_name, cgroup_base_path, complete_cgroup_path, verbose)

def get_throttling_percentage(namespace: Optional[str] = None,
                            container_name: Optional[str] = None,
                            label_selector: Optional[str] = None,
                            kubeconfig_path: Optional[str] = None,
                            cgroup_base_path: Optional[str] = None,
                            complete_cgroup_path: Optional[str] = None,
                            wait_seconds: Optional[float] = None,
                            verbose: bool = False) -> Dict:
    try:
        config.load_kube_config(kubeconfig_path) if kubeconfig_path else config.load_kube_config()
        debug_print("\nUsing default Kubernetes configuration from: ~/.kube/config", verbose)
    except Exception as e:
        return {
            "status": "error",
            "timestamp": time.time(),
            "error": f"Failed to load Kubernetes configuration: {str(e)}"
        }

    v1 = client.CoreV1Api()
    
    try:
        pods = v1.list_namespaced_pod(namespace, label_selector=label_selector).items
        debug_print(f"\nFound {len(pods)} pods matching label selector", verbose)
        
        if not pods:
            return {
                "status": "success",
                "timestamp": time.time(),
                "message": "No pods found matching the criteria",
                "pods": []
            }

        total_throttling_percentage = 0
        pod_results = []

        for pod in pods:
            initial_stats = get_container_cpu_stats(pod.metadata.name, container_name, namespace,
                                                  cgroup_base_path, complete_cgroup_path, verbose)
            
            if wait_seconds:
                debug_print(f"\nWaiting {wait_seconds} seconds for second measurement...", verbose)
                time.sleep(wait_seconds)
                final_stats = get_container_cpu_stats(pod.metadata.name, container_name, namespace,
                                                    cgroup_base_path, complete_cgroup_path, verbose)
            else:
                final_stats = initial_stats

            if initial_stats is None:
                debug_print(f"Warning: Could not get CPU stats for pod '{pod.metadata.name}'. Setting throttling to 0%.", verbose)
                pod_result = {
                    "pod_name": pod.metadata.name,
                    "throttling_percentage": 0,
                    "throttled_rate": 0,
                    "nr_periods": 0,
                    "nr_throttled": 0,
                    "cgroup_path": "unknown"
                }
                pod_results.append(pod_result)
                continue

            if wait_seconds and final_stats:
                periods_delta = final_stats['nr_periods'] - initial_stats['nr_periods']
                throttled_delta = final_stats['nr_throttled'] - initial_stats['nr_throttled']

                if periods_delta > 0:
                    throttling_percentage = (throttled_delta / periods_delta) * 100
                else:
                    debug_print(f"No new CPU periods for pod '{pod.metadata.name}'. Setting throttling to 0%.", verbose)
                    throttling_percentage = 0
                stats_to_use = final_stats
            else:
                if initial_stats['nr_periods'] > 0:
                    throttling_percentage = (initial_stats['nr_throttled'] / initial_stats['nr_periods']) * 100
                else:
                    debug_print(f"No CPU periods recorded for pod '{pod.metadata.name}'. Setting throttling to 0%.", verbose)
                    throttling_percentage = 0
                stats_to_use = initial_stats

            pod_result = {
                "pod_name": pod.metadata.name,
                "throttling_percentage": throttling_percentage,
                "throttled_rate": throttling_percentage,
                "nr_periods": stats_to_use['nr_periods'],
                "nr_throttled": stats_to_use['nr_throttled'],
                "cgroup_path": stats_to_use.get('cgroup_path_used', 'unknown')
            }

            if wait_seconds and final_stats:
                pod_result.update({
                    "periods_delta": periods_delta,
                    "throttled_delta": throttled_delta
                })

            pod_results.append(pod_result)
            debug_print(f"\nPod '{pod.metadata.name}':", verbose)
            debug_print(f"  CPU Throttling: {throttling_percentage:.2f}%", verbose)
            debug_print(f"  Throttled Rate: {throttling_percentage:.2f}", verbose)

        return {
            "status": "success",
            "timestamp": time.time(),
            "message": "CPU throttling analysis completed",
            "pods": pod_results
        }

    except Exception as e:
        return {
            "status": "error",
            "timestamp": time.time(),
            "error": f"Failed to analyze CPU throttling: {str(e)}"
        }

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
            "message": "CPU throttling analysis completed",
            "pods": result["pods"]
        }
        print(json.dumps(output, indent=2))

if __name__ == "__main__":
    main()
