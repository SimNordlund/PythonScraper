# scraper/models.py
from django.db import models

# ─────────── RESULTAT ───────────
class HorseResult(models.Model):
    id = models.BigAutoField(primary_key=True)

    # nycklar
    datum   = models.IntegerField(db_column="datum")          # //Changed!
    bankod  = models.CharField(max_length=2, db_column="bankod")  # //Changed!
    lopp    = models.IntegerField(db_column="lopp")           # //Changed!
    nr      = models.IntegerField(db_column="nr")             # //Changed!
    namn    = models.CharField(max_length=50, db_column="namn")   # //Changed!

    # position
    distans = models.IntegerField(null=True, blank=True, db_column="distans")  # //Changed!
    tillagg = models.IntegerField(null=True, blank=True, db_column="tillagg")  # //Changed!
    spar    = models.IntegerField(null=True, blank=True, db_column="spar")     # //Changed!

    # tider
    placering   = models.IntegerField(null=True, blank=True, db_column="placering")  # //Changed!
    tid         = models.FloatField(null=True, blank=True, db_column="tid")          # //Changed!
    startmetod  = models.CharField(max_length=1, blank=True, db_column="startmetod") # //Changed!
    galopp      = models.CharField(max_length=1, blank=True, db_column="galopp")     # //Changed!
    underlag    = models.CharField(max_length=1, blank=True, db_column="underlag")   # //Changed!

    ny_tid      = models.FloatField(null=True, blank=True, db_column="nytid")        # //Changed!
    diff_tid    = models.FloatField(null=True, blank=True, db_column="difftid")      # //Changed!
    diff_vinst  = models.FloatField(null=True, blank=True, db_column="diffvinst")    # //Changed!
    diff_medel  = models.FloatField(null=True, blank=True, db_column="diffmedel")    # //Changed!

    # övrigt
    sortering        = models.IntegerField(null=True, blank=True, db_column="sortering")        # //Changed!
    pris             = models.IntegerField(null=True, blank=True, db_column="pris")             # //Changed!
    tillagg_tid      = models.FloatField(null=True, blank=True, db_column="tillaggtid")         # //Changed!
    lopp_tid         = models.FloatField(null=True, blank=True, db_column="lopptid")            # //Changed!
    sortering_plac   = models.IntegerField(null=True, blank=True, db_column="sorteringplac")    # //Changed!
    sortering_tid    = models.IntegerField(null=True, blank=True, db_column="sorteringtid")     # //Changed!
    sortering_pris   = models.IntegerField(null=True, blank=True, db_column="sorteringpris")    # //Changed!
    sortering_klass  = models.IntegerField(null=True, blank=True, db_column="sorteringklass")   # //Changed!
    lopp_klass       = models.FloatField(null=True, blank=True, db_column="loppklass")          # //Changed!

    class Meta:
        db_table = "resultat"
        unique_together = ("datum", "bankod", "lopp", "nr")
        ordering = ("datum", "bankod", "lopp", "placering")

    def __str__(self):
        return f"{self.datum} L{self.lopp} #{self.nr} {self.namn}"


# ─────────── STARTLISTA ───────────
class StartList(models.Model):
    id = models.BigAutoField(primary_key=True)

    startdatum = models.IntegerField(db_column="startdatum")   # //Changed!
    bankod     = models.CharField(max_length=2, db_column="bankod")  # //Changed!
    lopp       = models.IntegerField(db_column="lopp")         # //Changed!
    nr         = models.IntegerField(db_column="nr")           # //Changed!
    namn       = models.CharField(max_length=50, db_column="namn")   # //Changed!
    spar       = models.IntegerField(null=True, blank=True, db_column="spar")       # //Changed!
    distans    = models.IntegerField(null=True, blank=True, db_column="distans")    # //Changed!
    kusk       = models.CharField(max_length=120, db_column="kusk")                 # //Changed!

    class Meta:
        db_table = "startlista"
        unique_together = ("startdatum", "bankod", "lopp", "nr")
        ordering = ("startdatum", "bankod", "lopp", "nr") 

    def __str__(self):                                              
        return f"{self.startdatum} L{self.lopp} #{self.nr} {self.namn}" 
