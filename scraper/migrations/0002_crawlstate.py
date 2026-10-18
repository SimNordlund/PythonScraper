from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("scraper", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="CrawlState",
            fields=[
                ("key", models.CharField(max_length=32, primary_key=True, serialize=False)),
                ("data", models.JSONField(default=dict)),
            ],
            options={"db_table": "scraper_crawl_state"},
        ),
    ]
