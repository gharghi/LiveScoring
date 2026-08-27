#!/bin/bash
# Quick Deployment Script - UPSERT + Archive Optimization
# Server: 91.107.241.31
#
# Usage:
#   ssh user@91.107.241.31
#   cd /path/to/LIveScoring
#   bash DEPLOYMENT_QUICK_START.sh
#

set -e  # Exit on error

echo "=============================================================================="
echo "Live Scoring Optimization Deployment - 91.107.241.31"
echo "=============================================================================="
echo ""

# Check if in correct directory
if [ ! -f "manage.py" ]; then
    echo "ERROR: manage.py not found. Are you in the correct directory?"
    exit 1
fi

# Step 1: Activate virtual environment
echo "Step 1: Activating virtual environment..."
if [ -d "venv" ]; then
    source venv/bin/activate
    echo "✓ Virtual environment activated"
else
    echo "✗ Virtual environment not found. Please activate manually."
    exit 1
fi

# Step 2: Verify Python and Django
echo ""
echo "Step 2: Verifying Python and Django..."
python --version
python manage.py --version
echo "✓ Python and Django verified"

# Step 3: Pull latest code
echo ""
echo "Step 3: Pulling latest code from git..."
git pull origin main
echo "✓ Latest code pulled"

# Step 4: Backup database
echo ""
echo "Step 4: Backing up database..."
BACKUP_FILE="/tmp/livescoring_backup_$(date +%s).sql"
pg_dump -U postgres -h localhost livescoring > "$BACKUP_FILE"
echo "✓ Database backed up to: $BACKUP_FILE"

# Step 5: Run migration
echo ""
echo "Step 5: Running database migration..."
echo "This may take 1-2 minutes to create indexes..."
python manage.py migrate
echo "✓ Migration completed"

# Step 6: Verify migration
echo ""
echo "Step 6: Verifying migration..."
python manage.py showmigrations live_api | grep 0006
echo "✓ Migration 0006 verified"

# Step 7: Check schema
echo ""
echo "Step 7: Checking database schema..."
psql -U postgres -h localhost livescoring -c \
    "\d live_api_trackingpoint" | head -5
echo "✓ Schema verified"

# Step 8: Stop application
echo ""
echo "Step 8: Stopping application..."
sudo systemctl stop livescoring
echo "✓ Application stopped"

# Step 9: Start application
echo ""
echo "Step 9: Starting application..."
sudo systemctl start livescoring
sleep 3
sudo systemctl status livescoring
echo "✓ Application started"

# Step 10: Set up cron job
echo ""
echo "Step 10: Setting up archival cron job..."
echo ""
echo "To add the daily archival job, run:"
echo "  sudo crontab -e"
echo ""
echo "And add this line (archive tasks finished > 1 day ago at 2 AM UTC):"
echo "  0 2 * * * cd $(pwd) && $(which python) manage.py archive_tracking_points --days-old 1 >> /var/log/livescoring_archive.log 2>&1"
echo ""
echo "Optional: Weekly cleanup (archive 7+ days old every Sunday at 3 AM):"
echo "  0 3 * * 0 cd $(pwd) && $(which python) manage.py archive_tracking_points --days-old 7 >> /var/log/livescoring_archive_weekly.log 2>&1"
echo ""

# Step 11: Test archival command
echo "Step 11: Testing archival command..."
echo ""
echo "Dry run (preview what would be archived):"
python manage.py archive_tracking_points --dry-run --days-old 1 | head -5
echo ""

# Step 12: Show database sizes
echo "Step 12: Current database status..."
psql -U postgres -h localhost livescoring -c \
    "SELECT
        'Main table' as table_name,
        pg_size_pretty(pg_total_relation_size('live_api_trackingpoint')) as size,
        (SELECT COUNT(*) FROM live_api_trackingpoint) as row_count
    UNION ALL
    SELECT
        'Archive table',
        pg_size_pretty(pg_total_relation_size('live_api_trackingpoint_archive')),
        (SELECT COUNT(*) FROM live_api_trackingpoint_archive)"

echo ""
echo "=============================================================================="
echo "Deployment Complete!"
echo "=============================================================================="
echo ""
echo "Next steps:"
echo "  1. Add cron job with: sudo crontab -e"
echo "  2. Test API performance with test batch"
echo "  3. Verify application logs: sudo tail -f /var/log/livescoring/app.log"
echo "  4. Monitor archival job: sudo tail -f /var/log/livescoring_archive.log"
echo ""
echo "Expected performance improvement:"
echo "  Before: 5-6 seconds per 1400-point batch"
echo "  After:  0.5-1 second per 1400-point batch (80% faster!)"
echo ""
echo "Backup location (if needed for rollback): $BACKUP_FILE"
echo ""
