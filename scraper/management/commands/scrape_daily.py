from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the daily startlist and results scrapers."

    def handle(self, *args, **options):
        self.stdout.write("Running scrape_startlist...")
        call_command("scrape_startlist")

        self.stdout.write("Running scrape_results...")
        call_command("scrape_results")

        self.stdout.write(self.style.SUCCESS("Done. scrape_startlist and scrape_results completed."))
