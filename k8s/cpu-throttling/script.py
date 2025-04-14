#!/usr/bin/env python3

import os
import sys
import time
import argparse
import json
from typing import Dict, List, Optional, Union
import traceback

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

def get_kubernetes_client(verbose: bool = False):
    """Get Kubernetes client using either in-cluster config or kubeconfig"""
    try:
        # Try in-cluster configuration first
        config.load_incluster_config()
        debug_print("\nUsing in-cluster configuration", verbose)
        return client.CoreV1Api()
    except config.ConfigException:
        try:
            # Fall back to kubeconfig
            config.load_kube_config()
            debug_print("\nUsing default Kubernetes configuration from: ~/.kube/config", verbose)
            return client.CoreV1Api()
        except Exception as e:
            raise Exception(f"Failed to load Kubernetes configuration: {str(e)}")

def exec_in_container(v1: client.CoreV1Api,
                    namespace: str,
                    pod_name: str,
                    container_name: str,
                    command: Union[str, List[str]],
                    verbose: bool = False) -> Optional[str]:
    """Execute a command in a container and return its output"""
    try:
        debug_print(f"Executing command in container: {command}", verbose)
        
        # Try to get pod info first to check if pod exists and is running
        try:
            pod = v1.read_namespaced_pod(pod_name, namespace)
            if pod.status.phase != 'Running':
                raise Exception(f"Pod {pod_name} is not running (current phase: {pod.status.phase})")
            
            # Check if container exists and is ready
            container_found = False
            container_ready = False
            for container_status in pod.status.container_statuses:
                if container_status.name == container_name:
                    container_found = True
                    container_ready = container_status.ready
                    if not container_ready:
                        raise Exception(f"Container {container_name} is not ready")
                    break
            
            if not container_found:
                raise Exception(f"Container {container_name} not found in pod {pod_name}")
            
        except client.ApiException as e:
            if e.status == 404:
                raise Exception(f"Pod {pod_name} not found in namespace {namespace}")
            raise Exception(f"Failed to get pod info: {str(e)}")

        # Now try to exec into the container
        try:
            resp = stream.stream(
                v1.connect_get_namespaced_pod_exec,
                pod_name,
                namespace,
                container=container_name,
                command=command if isinstance(command, list) else ["/bin/sh", "-c", command],
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False
            )
            return resp
            
        except client.ApiException as e:
            if e.status == 403:
                raise Exception(f"Permission denied to exec into container {container_name}")
            elif e.status == 400:
                raise Exception(f"Invalid container name or command: {str(e)}")
            else:
                raise Exception(f"Failed to exec into container: {str(e)}")
            
    except Exception as e:
        debug_print(f"Error executing command in container: {str(e)}", verbose)
        if verbose:
            debug_print(traceback.format_exc(), verbose)
        raise

def get_cpu_stats(v1: client.CoreV1Api,
                  namespace: str,
                  pod_name: str,
                  container_name: str,
                  cgroup_base_path: Optional[str] = None,
                  complete_cgroup_path: Optional[str] = None,
                  verbose: bool = False) -> Optional[Dict]:
    """Get CPU throttling stats from a container's cgroup"""
    try:
        # Command to find and read cpu.stat file
        cmd = (
            "sh -c 'if [ -f /sys/fs/cgroup/cpu.stat ]; then "
            "echo \"Found: /sys/fs/cgroup/cpu.stat\"; "
            "cat /sys/fs/cgroup/cpu.stat; "
            "exit 0; "
            "else echo \"No cpu.stat found\"; exit 1; fi'"
        )

        try:
            output = exec_in_container(v1, namespace, pod_name, container_name, cmd, verbose)
            if not output:
                raise Exception("No output received from container")
        except Exception as e:
            raise Exception(f"Failed to execute in container: {str(e)}")

        # First check if we got a "No cpu.stat found" message
        if "No cpu.stat found" in output:
            # Try to list available files to help with debugging
            try:
                ls_cmd = "sh -c 'ls -R /sys/fs/cgroup/'"
                ls_output = exec_in_container(v1, namespace, pod_name, container_name, ls_cmd, verbose)
                debug_print("\nAvailable files in /sys/fs/cgroup:", verbose)
                debug_print(ls_output, verbose)
            except Exception as ls_err:
                debug_print(f"\nFailed to list cgroup files: {str(ls_err)}", verbose)
            raise Exception(f"Could not find cpu.stat file in container {container_name} of pod {pod_name}")

        # Parse the output to find the cgroup path
        cgroup_path = None
        for line in output.splitlines():
            if line.startswith('Found:'):
                cgroup_path = line.split(':', 1)[1].strip()
                break

        if not cgroup_path:
            raise Exception(f"Could not determine cgroup path in container {container_name} of pod {pod_name}")

        # Initialize stats dictionary
        stats = {
            'nr_periods': 0,
            'nr_throttled': 0,
            'throttled_time': 0,
            'cgroup_path_used': cgroup_path
        }

        # Parse CPU stats
        found_any_stat = False
        for line in output.splitlines():
            if ':' not in line and ' ' not in line:
                continue

            try:
                if ' ' in line:
                    key, value = map(str.strip, line.split(' ', 1))
                else:
                    key, value = map(str.strip, line.split(':', 1))

                if key == 'nr_periods':
                    stats['nr_periods'] = int(value)
                    found_any_stat = True
                elif key == 'nr_throttled':
                    stats['nr_throttled'] = int(value)
                    found_any_stat = True
                elif key == 'throttled_usec':
                    stats['throttled_time'] = int(value)
                    found_any_stat = True
            except ValueError as e:
                raise Exception(f"Invalid value in cpu.stat file for {key}: {value}")

        if not found_any_stat:
            raise Exception(f"Could not read CPU stats from {cgroup_path} in container {container_name} of pod {pod_name}")

        debug_print(f"\nProcessed CPU stats for pod {pod_name}:", verbose)
        debug_print(f"  Nr Periods: {stats['nr_periods']}", verbose)
        debug_print(f"  Nr Throttled: {stats['nr_throttled']}", verbose)
        debug_print(f"  Throttled Time: {stats['throttled_time']} us", verbose)
        debug_print(f"  Cgroup Path: {stats.get('cgroup_path_used', 'unknown')}", verbose)

        return stats

    except Exception as e:
        debug_print(f"\nError getting CPU stats for pod {pod_name}: {str(e)}", verbose)
        if verbose:
            debug_print(traceback.format_exc(), verbose)
        raise Exception(f"Failed to get CPU stats: {str(e)}")

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
        # Get values from parameters or environment variables
        namespace = namespace or os.getenv(ENV_NAMESPACE)
        container_name = container_name or os.getenv(ENV_CONTAINER_NAME)
        label_selector = label_selector or os.getenv(ENV_LABEL_SELECTOR)
        
        # Validate required parameters
        if not namespace:
            return {
                "status": "error",
                "timestamp": time.time(),
                "error": "Namespace is required. Provide it via --namespace flag or NAMESPACE environment variable."
            }
            
        if not container_name:
            return {
                "status": "error",
                "timestamp": time.time(),
                "error": "Container name is required. Provide it via --container-name flag or CONTAINER_NAME environment variable."
            }
            
        if not label_selector:
            return {
                "status": "error",
                "timestamp": time.time(),
                "error": "Label selector is required. Provide it via --label-selector flag or LABEL_SELECTOR environment variable."
            }

        debug_print("\nConfiguration:", verbose)
        debug_print(f"Namespace: {namespace}", verbose)
        debug_print(f"Container Name: {container_name}", verbose)
        debug_print(f"Label Selector: {label_selector}", verbose)
        debug_print(f"Kubeconfig Path: {kubeconfig_path}", verbose)
        debug_print(f"Cgroup Base Path: {cgroup_base_path}", verbose)
        debug_print(f"Complete Cgroup Path: {complete_cgroup_path}", verbose)
        debug_print(f"Wait Seconds: {wait_seconds}", verbose)

        v1 = get_kubernetes_client(verbose)
        
        pods = v1.list_namespaced_pod(namespace=namespace, label_selector=label_selector).items
        debug_print(f"\nFound {len(pods)} pods matching label selector", verbose)
        
        if not pods:
            return {
                "status": "success",
                "timestamp": time.time(),
                "message": "No pods found matching the criteria",
                "pods": []
            }

        pod_results = []

        for pod in pods:
            try:
                initial_stats = get_container_cpu_stats(pod.metadata.name, container_name, namespace,
                                                      cgroup_base_path, complete_cgroup_path, verbose)
                
                if wait_seconds:
                    debug_print(f"\nWaiting {wait_seconds} seconds for second measurement...", verbose)
                    time.sleep(wait_seconds)
                    final_stats = get_container_cpu_stats(pod.metadata.name, container_name, namespace,
                                                        cgroup_base_path, complete_cgroup_path, verbose)
                else:
                    final_stats = initial_stats

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

            except Exception as e:
                return {
                    "status": "error",
                    "timestamp": time.time(),
                    "error": f"Failed to get CPU stats for pod {pod.metadata.name}: {str(e)}"
                }

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
    parser.add_argument('--json', action='store_true',
                       help='Output detailed JSON instead of just the throttling percentage')

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

    # Handle output based on mode
    if args.json:
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
    else:
        if "error" in result:
            print(result["error"], file=sys.stderr)
            sys.exit(1)
        else:
            # For float output, we'll use the average throttling percentage if multiple pods are found
            if result["pods"]:
                total_percentage = sum(pod["throttling_percentage"] for pod in result["pods"])
                avg_percentage = total_percentage / len(result["pods"])
                print(f"{avg_percentage:.6f}")
            else:
                print("0.000000")

if __name__ == "__main__":
    main()
