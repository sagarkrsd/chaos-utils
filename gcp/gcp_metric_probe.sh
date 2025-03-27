#!/bin/bash

# Script: gcp_metric_probe.sh
# Description: Fetch metrics from GCP Cloud Monitoring using PromQL for Harness Chaos Engineering
# Usage: ./gcp_metric_probe.sh --project=<project-id> --query="<promql-query>" --threshold=<threshold-value>

set -euo pipefail
IFS=$'\n\t'

# Default configuration
readonly LOG_FILE="/tmp/chaos_probe.log"
readonly WINDOW_MINUTES=${WINDOW_MINUTES:-5}
readonly CURL_TIMEOUT=${CURL_TIMEOUT:-10}

# Debug mode (can be set via environment variable)
DEBUG=${DEBUG:-false}

# Authentication configuration (in order of precedence)
# 1. GCP_AUTH_TOKEN environment variable
# 2. --token parameter
# 3. Token file specified by GCP_TOKEN_FILE environment variable
# 4. Token file specified by --token-file parameter
# 5. Service account key file specified by GOOGLE_APPLICATION_CREDENTIALS
# 6. gcloud auth (interactive)

# Ensure required commands exist
check_dependencies() {
    local missing_deps=()
    for cmd in jq curl bc awk date; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            missing_deps+=("$cmd")
        fi
    done

    # Only check for gcloud if we're using it for auth
    if [ -z "${GCP_AUTH_TOKEN:-}" ] && [ -z "${TOKEN:-}" ] && [ -z "${GCP_TOKEN_FILE:-}" ] && [ -z "${TOKEN_FILE:-}" ] && [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
        if ! command -v gcloud >/dev/null 2>&1; then
            missing_deps+=("gcloud")
        fi
    fi

    if [ ${#missing_deps[@]} -gt 0 ]; then
        echo "Error: Missing required commands: ${missing_deps[*]}" >&2
        exit 1
    fi
}

# Logging functions
log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" | tee -a "${LOG_FILE}"
}

log_error() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] ERROR: $*" | tee -a "${LOG_FILE}" >&2
}

log_debug() {
    if [ "${DEBUG}" = "true" ]; then
        echo "[$(date +'%Y-%m-%d %H:%M:%S')] DEBUG: $*" | tee -a "${LOG_FILE}"
    fi
}

# Cleanup function
cleanup() {
    if [ -n "${TEMP_FILE:-}" ] && [ -f "${TEMP_FILE}" ]; then
        rm -f "${TEMP_FILE}"
    fi
}

# Error handler
error_handler() {
    local line_no=$1
    local error_code=$2
    log_error "Error occurred in script at line: ${line_no}, error code: ${error_code}"
    cleanup
    exit "${error_code}"
}

# Set up error handling
trap 'error_handler ${LINENO} $?' ERR
trap cleanup EXIT

# Help message
usage() {
    cat <<'EOF'
Usage: ./gcp_metric_probe.sh --project=<project-id> --query="<promql-query>" --threshold=<threshold-value> [options]

Required:
    --project     GCP project ID
    --query       PromQL query (see examples below)
    --threshold   Threshold value to compare against

Authentication Options (in order of precedence):
    GCP_AUTH_TOKEN env var            Direct token string
    --token                           Direct token string
    GCP_TOKEN_FILE env var           Path to file containing token
    --token-file                      Path to file containing token
    GOOGLE_APPLICATION_CREDENTIALS    Path to service account key file
    gcloud auth (default)            Use gcloud authentication

Other Options:
    --window      Time window in minutes (default: 5)
    --debug       Enable debug logging
    --help        Show this help message

Example PromQL Queries:
    CPU Usage:
    "rate(container_cpu_usage_seconds_total{container=\"your-service\"}[5m])"

    Memory Usage:
    "container_memory_usage_bytes{container=\"your-service\"}"

    Network Received:
    "rate(container_network_receive_bytes_total{container=\"your-service\"}[5m])"

    Disk Usage:
    "container_fs_usage_bytes{container=\"your-service\"}"

Security Note:
    When using token authentication, ensure the token is properly secured.
    Avoid passing tokens directly on the command line in production environments.
    Prefer using token files or service account authentication.
EOF
    exit 1
}

# Function to read token from file
read_token_file() {
    local file="$1"
    if [ ! -f "$file" ]; then
        log_error "Token file not found: $file"
        return 1
    fi

    if [ ! -r "$file" ]; then
        log_error "Token file not readable: $file"
        return 1
    fi

    # Ensure file permissions are secure
    local perms
    perms=$(stat -f "%Lp" "$file" 2>/dev/null || stat -c "%a" "$file")
    if [ "$perms" != "600" ] && [ "$perms" != "400" ]; then
        log_error "Token file has insecure permissions: $file (should be 400 or 600)"
        return 1
    fi

    token=$(cat "$file")
    echo "$token"
}

# Function to get authentication token
get_auth_token() {
    local token

    # Check authentication methods in order of precedence
    if [ -n "${GCP_AUTH_TOKEN:-}" ]; then
        log_debug "Using token from GCP_AUTH_TOKEN environment variable"
        token="$GCP_AUTH_TOKEN"
    elif [ -n "${TOKEN:-}" ]; then
        log_debug "Using token from --token parameter"
        token="$TOKEN"
    elif [ -n "${GCP_TOKEN_FILE:-}" ]; then
        log_debug "Using token from file specified by GCP_TOKEN_FILE"
        token=$(read_token_file "$GCP_TOKEN_FILE") || return 1
    elif [ -n "${TOKEN_FILE:-}" ]; then
        log_debug "Using token from file specified by --token-file"
        token=$(read_token_file "$TOKEN_FILE") || return 1
    elif [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
        log_debug "Using service account credentials"
        token=$(gcloud auth application-default print-access-token) || return 1
    else
        log_debug "Using gcloud authentication"
        token=$(gcloud auth print-access-token) || return 1
    fi

    if [ -z "$token" ]; then
        log_error "Failed to obtain authentication token"
        return 1
    fi

    echo "$token"
}

# Function to URL encode a string
urlencode() {
    local string="$1"
    local strlen=${#string}
    local encoded=""
    local pos c o

    for (( pos=0 ; pos<strlen ; pos++ )); do
        c=${string:$pos:1}
        case "$c" in
            [-_.~a-zA-Z0-9] ) o="${c}" ;;
            * )               printf -v o '%%%02x' "'$c"
        esac
        encoded+="${o}"
    done
    echo "${encoded}"
}

# Parse and validate arguments
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --project=*)
                PROJECT_ID="${1#*=}"
                ;;
            --query=*)
                PROMQL_QUERY="${1#*=}"
                ;;
            --threshold=*)
                THRESHOLD="${1#*=}"
                ;;
            --token=*)
                TOKEN="${1#*=}"
                ;;
            --token-file=*)
                TOKEN_FILE="${1#*=}"
                ;;
            --window=*)
                WINDOW_MINUTES="${1#*=}"
                ;;
            --debug)
                DEBUG=true
                ;;
            --help)
                usage
                ;;
            *)
                log_error "Unknown parameter: $1"
                usage
                ;;
        esac
        shift
    done

    # Validate required parameters
    if [ -z "${PROJECT_ID:-}" ] || [ -z "${PROMQL_QUERY:-}" ] || [ -z "${THRESHOLD:-}" ]; then
        log_error "Missing required parameters"
        usage
    fi

    # Validate numeric values
    if ! echo "${THRESHOLD}" | grep -qE '^[0-9]+\.?[0-9]*$'; then
        log_error "Threshold must be a number"
        exit 1
    fi

    if ! echo "${WINDOW_MINUTES}" | grep -qE '^[0-9]+$'; then
        log_error "Window minutes must be a positive integer"
        exit 1
    fi
}

# Function to fetch metric from GCP using PromQL
fetch_metric() {
    local http_code

    end_time=$(date +%s)
    start_time=$((end_time - WINDOW_MINUTES * 60))
    
    log_debug "Executing PromQL query: ${PROMQL_QUERY}"
    log_debug "Time range: ${start_time} to ${end_time}"
    
    # Get authentication token
    token=$(get_auth_token) || {
        log_error "Failed to get authentication token"
        return 1
    }

    # Create temporary file for response
    TEMP_FILE=$(mktemp) || {
        log_error "Failed to create temporary file"
        return 1
    }

    # Prepare URL
    local base_url="${API_URL:-https://monitoring.googleapis.com/v1/projects/${PROJECT_ID}/location/global/prometheus/api/v1/query}"
    local encoded_query
    encoded_query=$(urlencode "${PROMQL_QUERY}")
    local url="${base_url}?query=${encoded_query}&time=${end_time}"

    log_debug "Request URL: ${url}"

    # Make API request using the Prometheus API endpoint
    http_code=$(curl -s -S \
        -w '%{http_code}' \
        -m "${CURL_TIMEOUT}" \
        -H "Authorization: Bearer ${token}" \
        -H "Content-Type: application/json" \
        -X GET \
        "${url}" \
        -o "${TEMP_FILE}" \
        2>/dev/null)

    if [ "${http_code}" != "200" ]; then
        log_error "API request failed with HTTP code: ${http_code}"
        log_error "Response: $(cat "${TEMP_FILE}")"
        return 1
    fi

    cat "${TEMP_FILE}"
}

# Function to extract and compare metric value from PromQL response
process_metric() {
    local response="$1"
    local value

    # Validate JSON response
    if ! echo "${response}" | jq empty 2>/dev/null; then
        log_error "Invalid JSON response"
        log_debug "Response: ${response}"
        return 1
    fi

    # Extract value from PromQL response format
    value=$(echo "${response}" | jq -r '
        if .data.result and (.data.result | length) > 0 then
            .data.result[0].value[1]
        else
            "null"
        end
    ')

    if [ "${value}" = "null" ]; then
        log_error "No metric data available"
        return 1
    fi

    # Convert scientific notation to decimal if needed
    value=$(echo "${value}" | awk '{printf "%.6f", $1}')

    log "Current value: ${value}"
    log "Threshold: ${THRESHOLD}"

    # Use bc for floating point comparison
    if [ "$(echo "${value} > ${THRESHOLD}" | bc -l)" -eq 1 ]; then
        log_error "Value (${value}) exceeds threshold (${THRESHOLD})"
        return 1
    fi

    log "Value within threshold"
    return 0
}

main() {
    # Check dependencies
    check_dependencies

    # Parse command line arguments
    parse_args "$@"

    log "Starting metric check..."
    
    # Fetch metric using PromQL
    response=$(fetch_metric) || exit 1
    
    # Process metric
    process_metric "${response}"
}

# Run main function with all arguments
main "$@"
