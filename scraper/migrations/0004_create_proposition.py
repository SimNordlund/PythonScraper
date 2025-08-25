# //Changed!
from django.db import migrations, models  # //Changed!

class Migration(migrations.Migration):  # //Changed!
    dependencies = [
        ("scraper", "0003_startlist"),  # //Changed!
    ]

    operations = [
        migrations.CreateModel(  # //Changed!
            name="Proposition",  # //Changed!
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),             # //Changed!
                ("startdatum", models.IntegerField(db_column="startdatum")),               # //Changed!
                ("bankod", models.CharField(max_length=2, db_column="bankod")),            # //Changed!
                ("namn", models.CharField(max_length=50, db_column="namn")),               # //Changed!
                ("proposition", models.IntegerField(db_column="proposition")),             # //Changed!
            ],
            options={
                "db_table": "proposition",                                                # //Changed!
                "ordering": ("startdatum", "bankod", "proposition", "namn"),              # //Changed!
                "unique_together": {("startdatum", "bankod", "namn", "proposition")},     # //Changed!
            },
        ),
    ]
