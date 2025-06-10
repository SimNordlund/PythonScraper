# scraper/models.py
from django.db import models


class HorseResult(models.Model):
    # ─────────────────────────────────────────────────────────────
    #  Primary-key column ― Django will add one anyway, so declare it
    # ─────────────────────────────────────────────────────────────
    id = models.BigAutoField(primary_key=True)                    # //Changed!

    # ── key columns ──────────────────────────────────────────────
    datum   = models.IntegerField(db_column="Datum")
    bankod  = models.CharField(max_length=2, db_column="Bankod")
    lopp    = models.IntegerField(db_column="Lopp")
    nr      = models.IntegerField(db_column="Nr")
    namn    = models.CharField(max_length=50, db_column="Namn")

    # ── distance / track position ────────────────────────────────
    distans = models.IntegerField(null=True, blank=True, db_column="Distans")
    tillagg = models.IntegerField(null=True, blank=True, db_column="Tillagg")
    spar    = models.IntegerField(null=True, blank=True, db_column="Spar")

    # ── result & time information ────────────────────────────────
    placering   = models.IntegerField(null=True, blank=True, db_column="Placering")
    tid         = models.FloatField(null=True, blank=True, db_column="Tid")
    startmetod  = models.CharField(max_length=1, blank=True, db_column="Startmetod")
    galopp      = models.CharField(max_length=1, blank=True, db_column="Galopp")
    underlag    = models.CharField(max_length=1, blank=True, db_column="Underlag")

    ny_tid      = models.FloatField(null=True, blank=True, db_column="NyTid")
    diff_tid    = models.FloatField(null=True, blank=True, db_column="DiffTid")
    diff_vinst  = models.FloatField(null=True, blank=True, db_column="DiffVinst")
    diff_medel  = models.FloatField(null=True, blank=True, db_column="DiffMedel")

    # ── sorting / miscellaneous ──────────────────────────────────
    sortering        = models.IntegerField(null=True, blank=True, db_column="Sortering")
    pris             = models.IntegerField(null=True, blank=True, db_column="Pris")
    tillagg_tid      = models.FloatField(null=True, blank=True, db_column="TillaggTid")
    lopp_tid         = models.FloatField(null=True, blank=True, db_column="LoppTid")

    sortering_plac   = models.IntegerField(null=True, blank=True, db_column="SorteringPlac")
    sortering_tid    = models.IntegerField(null=True, blank=True, db_column="SorteringTid")
    sortering_pris   = models.IntegerField(null=True, blank=True, db_column="SorteringPris")
    sortering_klass  = models.IntegerField(null=True, blank=True, db_column="SorteringKlass")
    lopp_klass       = models.FloatField(null=True, blank=True, db_column="LoppKlass")

    # ── meta info ────────────────────────────────────────────────
    class Meta:
        db_table        = "Resultat"
        # managed = False  ← REMOVED so Django owns the schema     # //Changed!
        unique_together = ("datum", "bankod", "lopp", "nr")
        ordering        = ("datum", "bankod", "lopp", "placering")

    def __str__(self):
        return f"{self.datum} L{self.lopp} #{self.nr} {self.namn}"

# ─────────────────────────  START-LISTA  ────────────────────────────
class StartList(models.Model):                                      # //Changed!
    id = models.BigAutoField(primary_key=True)                      # //Changed!

    startdatum = models.IntegerField(db_column="Startdatum")        # //Changed!
    bankod     = models.CharField(max_length=2, db_column="Bankod") # //Changed!
    lopp       = models.IntegerField(db_column="Lopp")              # //Changed!
    nr         = models.IntegerField(db_column="Nr")                # //Changed!
    namn       = models.CharField(max_length=50, db_column="Namn")  # //Changed!
    spar       = models.IntegerField(null=True, blank=True,
                                     db_column="Spar")              # //Changed!
    distans    = models.IntegerField(null=True, blank=True,
                                     db_column="Distans")           # //Changed!
    kusk       = models.CharField(max_length=120, db_column="Kusk") # //Changed!

    class Meta:                                                     # //Changed!
        db_table        = "Startlista"                              # //Changed!
        unique_together = ("startdatum", "bankod", "lopp", "nr")    # //Changed!
        ordering        = ("startdatum", "bankod", "lopp", "nr")    # //Changed!

    def __str__(self):                                              # //Changed!
        return f"{self.startdatum} L{self.lopp} #{self.nr} {self.namn}" # //Changed!
