
from django.db import migrations, models  

class Migration(migrations.Migration):  
    dependencies = [  
        ("scraper", "0004_create_proposition"),  
    ]  

    operations = [  
        migrations.AddField(  
            model_name="proposition",  
            name="distans",  
            field=models.IntegerField(null=True, blank=True, db_column="distans"),  
        ),  
        migrations.AddField(  
            model_name="proposition",  
            name="kuskanskemal",  
            field=models.CharField(max_length=120, null=True, blank=True, db_column="kuskanskemal"),  
        ),  
    ]  

