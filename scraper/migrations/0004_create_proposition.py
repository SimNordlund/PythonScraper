
from django.db import migrations, models  

class Migration(migrations.Migration):  
    dependencies = [
        ("scraper", "0003_startlist"),  
    ]

    operations = [
        migrations.CreateModel(  
            name="Proposition",  
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),             
                ("startdatum", models.IntegerField(db_column="startdatum")),               
                ("bankod", models.CharField(max_length=2, db_column="bankod")),            
                ("namn", models.CharField(max_length=50, db_column="namn")),               
                ("proposition", models.IntegerField(db_column="proposition")),             
            ],
            options={
                "db_table": "proposition",                                                
                "ordering": ("startdatum", "bankod", "proposition", "namn"),              
                "unique_together": {("startdatum", "bankod", "namn", "proposition")},     
            },
        ),
    ]
