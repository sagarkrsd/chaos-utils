#!/bin/bash

set -euo pipefail
IFS=$'\n\t'

# Oracle connection details with validation
ORACLE_USER=${ORACLE_USER:-}
ORACLE_PASSWORD=${ORACLE_PASSWORD:-}
ORACLE_HOST=${ORACLE_HOST:-localhost}
ORACLE_PORT=${ORACLE_PORT:-1521}
ORACLE_SID=${ORACLE_SID:-}
ORACLE_SERVICE=${ORACLE_SERVICE:-}
ORACLE_HOME=${ORACLE_HOME:-}
TNS_ADMIN=${TNS_ADMIN:-}

# Validate required environment variables
if [ -z "$ORACLE_USER" ]; then
    echo "Error: ORACLE_USER environment variable must be set"
    exit 1
fi

if [ -z "$ORACLE_PASSWORD" ]; then
    echo "Error: ORACLE_PASSWORD environment variable must be set"
    exit 1
fi

if [ -z "$ORACLE_SID" ] && [ -z "$ORACLE_SERVICE" ]; then
    echo "Error: Either ORACLE_SID or ORACLE_SERVICE must be set"
    exit 1
fi

if [ -z "$ORACLE_HOME" ]; then
    echo "Error: ORACLE_HOME environment variable must be set"
    exit 1
fi

# Validate Oracle port is numeric and within valid range
if ! [[ "$ORACLE_PORT" =~ ^[0-9]+$ ]] || [ "$ORACLE_PORT" -lt 1 ] || [ "$ORACLE_PORT" -gt 65535 ]; then
    echo "Error: ORACLE_PORT must be a valid port number (1-65535)"
    exit 1
fi

# Function to safely escape Oracle identifiers
escape_identifier() {
    if [ $# -ne 1 ]; then
        echo "Error: escape_identifier requires exactly one parameter"
        return 1
    fi
    local identifier=$1
    # Remove quotes and escape existing quotes
    echo "$identifier" | sed "s/'/''/g"
}

# Function to build Oracle connection string
get_connection_string() {
    local conn_str=""
    if [ -n "$ORACLE_SERVICE" ]; then
        conn_str="$ORACLE_USER/$ORACLE_PASSWORD@//$ORACLE_HOST:$ORACLE_PORT/$ORACLE_SERVICE"
    else
        conn_str="$ORACLE_USER/$ORACLE_PASSWORD@(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=$ORACLE_HOST)(PORT=$ORACLE_PORT))(CONNECT_DATA=(SID=$ORACLE_SID)))"
    fi
    echo "$conn_str"
}

# Function to test database connection
test_connection() {
    local max_retries=3
    local retry_count=0
    local retry_delay=2

    local conn_str
    conn_str=$(get_connection_string)

    while [ $retry_count -lt $max_retries ]; do
        if echo "SET PAGESIZE 0 FEEDBACK OFF VERIFY OFF HEADING OFF ECHO OFF
SELECT 1 FROM DUAL;
EXIT;" | "$ORACLE_HOME/bin/sqlplus" -S "$conn_str" > /dev/null 2>&1; then
            return 0
        fi
        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            echo "Warning: Connection attempt $retry_count failed, retrying in $retry_delay seconds..."
            sleep $retry_delay
        fi
    done
    
    echo "Error: Could not connect to Oracle database at $ORACLE_HOST:$ORACLE_PORT after $max_retries attempts"
    echo "Please check:"
    echo "1. Oracle database is running"
    echo "2. Credentials are correct"
    echo "3. Host and port are accessible"
    echo "4. SID/Service name is correct"
    echo "5. ORACLE_HOME is set correctly"
    echo "6. LD_LIBRARY_PATH includes Oracle libraries"
    return 1
}

# Function to list all schemas
list_schemas() {
    if ! test_connection; then
        return 1
    fi
    
    local conn_str
    conn_str=$(get_connection_string)

    echo "SET LINESIZE 1000
SET PAGESIZE 1000
SET FEEDBACK OFF
SET VERIFY OFF
SELECT username AS schema_name 
FROM dba_users 
WHERE account_status = 'OPEN' 
ORDER BY username;
EXIT;" | "$ORACLE_HOME/bin/sqlplus" -S "$conn_str"
}

# Function to show tables in a schema
show_tables() {
    if [ $# -ne 1 ]; then
        echo "Error: show_tables requires schema name parameter"
        echo "Usage: show_tables <schema_name>"
        return 1
    fi
    
    local schema=$1
    
    if ! test_connection; then
        return 1
    fi

    local escaped_schema
    if ! escaped_schema=$(escape_identifier "$schema"); then
        echo "Error: Failed to escape schema name"
        return 1
    fi

    local conn_str
    conn_str=$(get_connection_string)

    echo "SET LINESIZE 1000
SET PAGESIZE 1000
SET FEEDBACK OFF
SET VERIFY OFF
SELECT table_name 
FROM all_tables 
WHERE owner = UPPER('$escaped_schema')
ORDER BY table_name;
EXIT;" | "$ORACLE_HOME/bin/sqlplus" -S "$conn_str"
}

# Function to execute a query and print the results
execute_query() {
    if [ $# -lt 1 ]; then
        echo "Error: execute_query requires query parameter"
        echo "Usage: execute_query <query>"
        echo "Example: execute_query \"SELECT * FROM employees WHERE ROWNUM <= 10\""
        return 1
    fi

    local query=$1
    
    if ! test_connection; then
        return 1
    fi

    # Validate query is not empty
    if [ -z "$query" ]; then
        echo "Error: Query cannot be empty"
        return 1
    fi

    # Add ROWNUM if it's a SELECT query and doesn't already have ROWNUM or FETCH FIRST
    if [[ "$query" =~ ^[[:space:]]*SELECT[[:space:]] ]] && \
       [[ ! "$query" =~ ROWNUM[[:space:]]*"<="[[:space:]]*[0-9]+ ]] && \
       [[ ! "$query" =~ FETCH[[:space:]]+FIRST[[:space:]]+[0-9]+[[:space:]]+(ROWS?|ONLY) ]]; then
        query="$query WHERE ROWNUM <= 1000"
        echo "Note: Added ROWNUM <= 1000 to prevent excessive results"
    fi

    local conn_str
    conn_str=$(get_connection_string)

    echo "SET LINESIZE 1000
SET PAGESIZE 1000
SET FEEDBACK OFF
SET VERIFY OFF
$query;
EXIT;" | "$ORACLE_HOME/bin/sqlplus" -S "$conn_str"
}

# Function to check script dependencies
check_dependencies() {
    local missing_deps=0

    # Check if ORACLE_HOME/bin/sqlplus exists and is executable
    if [ ! -x "$ORACLE_HOME/bin/sqlplus" ]; then
        echo "Error: sqlplus not found at $ORACLE_HOME/bin/sqlplus or not executable"
        missing_deps=1
    fi

    # Check if TNS_ADMIN directory exists
    if [ ! -d "$TNS_ADMIN" ]; then
        echo "Warning: TNS_ADMIN directory ($TNS_ADMIN) does not exist"
        # Not a fatal error, as it might not be needed for basic connection
    fi

    # Check for required system utilities
    for cmd in tr sed grep; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            echo "Error: Required command '$cmd' not found"
            missing_deps=1
        fi
    done

    if [ $missing_deps -ne 0 ]; then
        echo "Error: Missing required dependencies"
        return 1
    fi
}

# Check dependencies when script is sourced
check_dependencies

# If this script is run directly (not sourced), show usage
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    echo "This script is meant to be sourced, not executed directly."
    echo "Usage: source $0"
    exit 1
fi
