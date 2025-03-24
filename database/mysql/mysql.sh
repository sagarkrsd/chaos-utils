#!/bin/bash

# Exit on error, unset variables, and pipe failures
set -euo pipefail
IFS=$'\n\t'

# MySQL login details with validation
MYSQL_USER=${MYSQL_USER:-}
MYSQL_PASSWORD=${MYSQL_PASSWORD:-}
MYSQL_HOST=${MYSQL_HOST:-localhost}
MYSQL_PORT=${MYSQL_PORT:-3306}

# Validate required environment variables
if [ -z "$MYSQL_USER" ]; then
    echo "Error: MYSQL_USER environment variable must be set"
    exit 1
fi

# Note: We allow empty password as it's a valid MySQL configuration

# Validate MySQL port is numeric and within valid range
if ! [[ "$MYSQL_PORT" =~ ^[0-9]+$ ]] || [ "$MYSQL_PORT" -lt 1 ] || [ "$MYSQL_PORT" -gt 65535 ]; then
    echo "Error: MYSQL_PORT must be a valid port number (1-65535)"
    exit 1
fi

# Function to safely escape MySQL identifiers
escape_identifier() {
    if [ $# -ne 1 ]; then
        echo "Error: escape_identifier requires exactly one parameter"
        return 1
    fi
    local identifier=$1
    # Remove backticks and escape existing backticks
    echo "$identifier" | sed 's/`/``/g'
}

# Function to safely escape MySQL string literals
escape_string() {
    if [ $# -ne 1 ]; then
        echo "Error: escape_string requires exactly one parameter"
        return 1
    fi
    local string=$1
    # Escape special characters
    echo "$string" | sed 's/[\\"]/\\&/g'
}

# Function to test database connection
test_connection() {
    local max_retries=3
    local retry_count=0
    local retry_delay=2

    # Build MySQL command based on whether password is empty
    local mysql_cmd="mysql --protocol=TCP -h \"$MYSQL_HOST\" -P \"$MYSQL_PORT\" -u \"$MYSQL_USER\""
    if [ -n "$MYSQL_PASSWORD" ]; then
        mysql_cmd="$mysql_cmd -p\"$MYSQL_PASSWORD\""
    fi

    while [ $retry_count -lt $max_retries ]; do
        if eval "$mysql_cmd -e \"SELECT 1\"" >/dev/null 2>&1; then
            return 0
        fi
        retry_count=$((retry_count + 1))
        if [ $retry_count -lt $max_retries ]; then
            echo "Warning: Connection attempt $retry_count failed, retrying in $retry_delay seconds..."
            sleep $retry_delay
        fi
    done
    
    echo "Error: Could not connect to MySQL server at $MYSQL_HOST:$MYSQL_PORT after $max_retries attempts"
    echo "Please check:"
    echo "1. MySQL server is running"
    echo "2. Credentials are correct"
    echo "3. Host and port are accessible"
    return 1
}

# Function to list all databases
list_databases() {
    if ! test_connection; then
        return 1
    fi
    
    # Build MySQL command based on whether password is empty
    local mysql_cmd="mysql --protocol=TCP -h \"$MYSQL_HOST\" -P \"$MYSQL_PORT\" -u \"$MYSQL_USER\""
    if [ -n "$MYSQL_PASSWORD" ]; then
        mysql_cmd="$mysql_cmd -p\"$MYSQL_PASSWORD\""
    fi
    
    eval "$mysql_cmd -e \"SHOW DATABASES\""
}

# Function to show tables in a database
show_tables() {
    if [ $# -ne 1 ]; then
        echo "Error: show_tables requires database name parameter"
        echo "Usage: show_tables <database_name>"
        return 1
    fi
    
    local database=$1
    
    if ! test_connection; then
        return 1
    fi
    
    local escaped_db
    if ! escaped_db=$(escape_identifier "$database"); then
        echo "Error: Failed to escape database name"
        return 1
    fi
    
    # Build MySQL command based on whether password is empty
    local mysql_cmd="mysql --protocol=TCP -h \"$MYSQL_HOST\" -P \"$MYSQL_PORT\" -u \"$MYSQL_USER\""
    if [ -n "$MYSQL_PASSWORD" ]; then
        mysql_cmd="$mysql_cmd -p\"$MYSQL_PASSWORD\""
    fi
    
    if ! eval "$mysql_cmd -e \"USE \\\`$escaped_db\\\`\"" 2>/dev/null; then
        echo "Error: Database '$database' does not exist"
        return 1
    fi
    
    eval "$mysql_cmd \"$database\" -e \"SHOW TABLES\""
}

# Function to execute a MySQL query and print the results
execute_query() {
    if [ $# -lt 2 ]; then
        echo "Error: execute_query requires query and database parameters"
        echo "Usage: execute_query <query> <database>"
        echo "Example: execute_query \"SELECT * FROM users LIMIT 10\" \"mydb\""
        return 1
    fi

    local query=$1
    local database=$2
    
    if ! test_connection; then
        return 1
    fi

    # Build MySQL command based on whether password is empty
    local mysql_cmd="mysql --protocol=TCP -h \"$MYSQL_HOST\" -P \"$MYSQL_PORT\" -u \"$MYSQL_USER\""
    if [ -n "$MYSQL_PASSWORD" ]; then
        mysql_cmd="$mysql_cmd -p\"$MYSQL_PASSWORD\""
    fi

    # Check if database exists
    local escaped_db
    if ! escaped_db=$(escape_identifier "$database"); then
        echo "Error: Failed to escape database name"
        return 1
    fi

    if ! eval "$mysql_cmd -e \"USE \\\`$escaped_db\\\`\"" 2>/dev/null; then
        echo "Error: Database '$database' does not exist"
        return 1
    fi
    
    # Validate query is not empty and contains SELECT, SHOW, or DESCRIBE
    if [ -z "$query" ]; then
        echo "Error: Query cannot be empty"
        return 1
    fi
    
    # Convert query to uppercase for comparison
    local query_upper
    query_upper=$(echo "$query" | tr '[:lower:]' '[:upper:]')
    
    if [[ ! "$query_upper" =~ ^[[:space:]]*(SELECT|SHOW|DESCRIBE|DESC)[[:space:]] ]]; then
        echo "Error: Only SELECT, SHOW, or DESCRIBE queries are allowed for safety"
        return 1
    fi

    # Add LIMIT if it's a SELECT query and doesn't already have a LIMIT clause
    if [[ "$query_upper" =~ ^[[:space:]]*SELECT[[:space:]] ]] && [[ ! "$query_upper" =~ LIMIT[[:space:]]+[0-9]+ ]]; then
        query="$query LIMIT 1000"
        echo "Note: Added LIMIT 1000 to prevent excessive results"
    fi
    
    # Execute query with error handling
    if ! eval "$mysql_cmd \"$database\" -e \"$query\"" 2>/dev/null; then
        echo "Error: Failed to execute query"
        echo "Query attempted: $query"
        echo "Make sure your query syntax is correct and you have proper permissions"
        return 1
    fi
}

# Function to check script dependencies
check_dependencies() {
    local missing_deps=0
    
    if ! command -v mysql >/dev/null 2>&1; then
        echo "Error: mysql client is not installed"
        missing_deps=1
    fi

    if ! command -v sed >/dev/null 2>&1; then
        echo "Error: sed is not installed"
        missing_deps=1
    fi

    if ! command -v tr >/dev/null 2>&1; then
        echo "Error: tr is not installed"
        missing_deps=1
    fi
    
    if [ $missing_deps -eq 1 ]; then
        exit 1
    fi
}

# Check dependencies when script is sourced
check_dependencies

# If this script is run directly (not sourced), show usage
if [ "${BASH_SOURCE[0]}" -ef "$0" ]; then
    echo "This script is meant to be sourced, not executed directly."
    echo "Usage: source ${BASH_SOURCE[0]}"
    echo "Required environment variables:"
    echo "  MYSQL_USER     - MySQL username"
    echo "  MYSQL_PASSWORD - MySQL password (can be empty)"
    echo "Optional environment variables:"
    echo "  MYSQL_HOST     - MySQL host (default: localhost)"
    echo "  MYSQL_PORT     - MySQL port (default: 3306)"
    echo "Available functions:"
    echo "  list_databases                     - List all accessible databases"
    echo "  show_tables <database_name>        - List all tables in the specified database"
    echo "  execute_query <query> <database>   - Execute a SELECT/SHOW/DESCRIBE query"
    exit 1
fi
