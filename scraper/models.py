from django.db import models

class HorseResult(models.Model):
    id = models.BigAutoField(primary_key=True)

    datum   = models.IntegerField(db_column="datum")
    bankod  = models.CharField(max_length=20, db_column="bankod")
    lopp    = models.IntegerField(db_column="lopp")
    nr      = models.IntegerField(db_column="nr")
    namn    = models.CharField(max_length=50, db_column="namn")

    distans = models.IntegerField(null=True, blank=True, db_column="distans")
    tillagg = models.IntegerField(null=True, blank=True, db_column="tillagg")
    spar    = models.IntegerField(null=True, blank=True, db_column="spar")

    placering   = models.IntegerField(null=True, blank=True, db_column="placering")
    tid         = models.FloatField(null=True, blank=True, db_column="tid")
    startmetod  = models.CharField(max_length=1, blank=True, db_column="startmetod")
    galopp      = models.CharField(max_length=1, blank=True, db_column="galopp")
    underlag    = models.CharField(max_length=2, blank=True, db_column="underlag")

    ny_tid      = models.FloatField(null=True, blank=True, db_column="nytid")
    diff_tid    = models.FloatField(null=True, blank=True, db_column="difftid")
    diff_vinst  = models.FloatField(null=True, blank=True, db_column="diffvinst")
    diff_medel  = models.FloatField(null=True, blank=True, db_column="diffmedel")

    sortering        = models.IntegerField(null=True, blank=True, db_column="sortering")
    pris             = models.IntegerField(null=True, blank=True, db_column="pris")
    tillagg_tid      = models.FloatField(null=True, blank=True, db_column="tillaggtid")
    lopp_tid         = models.FloatField(null=True, blank=True, db_column="lopptid")
    sortering_plac   = models.IntegerField(null=True, blank=True, db_column="sorteringplac")
    sortering_tid    = models.IntegerField(null=True, blank=True, db_column="sorteringtid")
    sortering_pris   = models.IntegerField(null=True, blank=True, db_column="sorteringpris")
    sortering_klass  = models.IntegerField(null=True, blank=True, db_column="sorteringklass")
    lopp_klass       = models.FloatField(null=True, blank=True, db_column="loppklass")
    
    odds = models.IntegerField(db_column="odds", default=999) 
    kusk = models.CharField(max_length=80, db_column="kusk", blank=True, default="")

    class Meta:
        db_table = "resultat"
        constraints = [  
            models.UniqueConstraint(  
                fields=("datum", "bankod", "lopp", "namn"),  
                name="uq_resultat_datum_bankod_lopp_namn",  
            ),
        ]
        ordering = ("datum", "bankod", "lopp", "placering")  


class StartList(models.Model):
    id = models.BigAutoField(primary_key=True)

    startdatum = models.IntegerField(db_column="startdatum")
    bankod     = models.CharField(max_length=2, db_column="bankod")
    lopp       = models.IntegerField(db_column="lopp")
    nr         = models.IntegerField(db_column="nr")
    namn       = models.CharField(max_length=50, db_column="namn")
    spar       = models.IntegerField(null=True, blank=True, db_column="spar")
    distans    = models.IntegerField(null=True, blank=True, db_column="distans")
    kusk       = models.CharField(max_length=120, db_column="kusk")

    class Meta:
        db_table = "startlista"
        unique_together = ("startdatum", "bankod", "lopp", "nr")
        ordering = ("startdatum", "bankod", "lopp", "nr")

    def __str__(self):
        return f"{self.startdatum} L{self.lopp} #{self.nr} {self.namn}"


class Proposition(models.Model):
    id          = models.BigAutoField(primary_key=True)
    startdatum  = models.IntegerField(db_column="startdatum")
    bankod      = models.CharField(max_length=2, db_column="bankod")
    namn        = models.CharField(max_length=50, db_column="namn")
    proposition = models.IntegerField(db_column="proposition")

    distans      = models.IntegerField(null=True, blank=True, db_column="distans")            
    kuskanskemal = models.CharField(max_length=120, null=True, blank=True, db_column="kuskanskemal")  

    class Meta:
        db_table = "proposition"
        ordering = ("startdatum", "bankod", "proposition", "namn")
        unique_together = ("startdatum", "bankod", "namn", "proposition")

    def __str__(self):
        return f"{self.startdatum} {self.bankod} {self.proposition} {self.namn}"
