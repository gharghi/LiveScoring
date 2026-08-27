"""Management command to archive old tracking points to separate table."""

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from live_api.models import Task, TrackingPoint
from live_api.storage import archive_task_points


class Command(BaseCommand):
    help = 'Archive tracking points from finished tasks to archive table'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days-old',
            type=int,
            default=1,
            help='Archive points from tasks finished N+ days ago (default: 1)'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be archived without actually doing it'
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Archive even active/running tasks (use with caution)'
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Archive only N tasks (useful for testing)'
        )

    def handle(self, *args, **options):
        days_old = options['days_old']
        dry_run = options['dry_run']
        force = options['force']
        limit = options['limit']

        cutoff_date = timezone.now() - timedelta(days=days_old)

        # Find finished tasks older than N days
        finished_tasks = Task.objects.filter(
            updated_at__lt=cutoff_date
        )

        if not force:
            finished_tasks = finished_tasks.exclude(
                tracking_points_archive__isnull=False  # Already archived
            )

        if limit:
            finished_tasks = finished_tasks[:limit]

        finished_tasks = list(finished_tasks)

        if not finished_tasks:
            self.stdout.write(self.style.WARNING("No tasks to archive"))
            return

        self.stdout.write(f"\nFound {len(finished_tasks)} task(s) to archive\n")

        total_archived = 0
        total_deleted = 0

        for idx, task in enumerate(finished_tasks, 1):
            # Count points
            point_count = TrackingPoint.objects.filter(task=task).count()

            if point_count == 0:
                self.stdout.write(f"{idx}. {task.external_manga_id or task.id}: 0 points (skipped)")
                continue

            if dry_run:
                self.stdout.write(
                    f"{idx}. {task.external_manga_id or task.id}: Would archive {point_count:,} points "
                    f"(finished {task.updated_at.strftime('%Y-%m-%d %H:%M:%S')})"
                )
                total_archived += point_count
            else:
                try:
                    result = archive_task_points(task, source='sql')
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"{idx}. {task.external_manga_id or task.id}: ✓ Archived {result['archived_count']:,} "
                            f"points (deleted {result['deleted_count']:,} from main table)"
                        )
                    )
                    total_archived += result['archived_count']
                    total_deleted += result['deleted_count']
                except Exception as e:
                    self.stdout.write(
                        self.style.ERROR(
                            f"{idx}. {task.external_manga_id or task.id}: ✗ Failed - {str(e)}"
                        )
                    )

        self.stdout.write("\n" + "=" * 70)
        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"DRY RUN: Would archive {total_archived:,} points")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"✓ Archived {total_archived:,} points total\n"
                    f"✓ Deleted {total_deleted:,} points from main table"
                )
            )

        # Show database sizes
        self._show_table_sizes()

    def _show_table_sizes(self):
        """Display current table sizes."""
        from django.db import connection

        if connection.vendor != 'postgresql':
            return

        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT
                    pg_size_pretty(pg_total_relation_size('live_api_trackingpoint')) as main_size,
                    (SELECT COUNT(*) FROM live_api_trackingpoint) as main_count,
                    pg_size_pretty(pg_total_relation_size('live_api_trackingpoint_archive')) as archive_size,
                    (SELECT COUNT(*) FROM live_api_trackingpoint_archive) as archive_count
            """)
            result = cursor.fetchone()
            main_size, main_count, archive_size, archive_count = result

            self.stdout.write("\n" + "=" * 70)
            self.stdout.write("Database Table Status:")
            self.stdout.write(f"  Main table:    {main_count:>10,} points  {main_size:>12}")
            self.stdout.write(f"  Archive table: {archive_count:>10,} points  {archive_size:>12}")
            self.stdout.write("=" * 70)
