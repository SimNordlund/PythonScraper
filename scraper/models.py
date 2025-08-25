# scraper/models.py
from django.db import models

class HorseResult(models.Model):
    id = models.BigAutoField(primary_key=True)

  
    datum   = models.IntegerField(db_column="datum")          
    bankod  = models.CharField(max_length=2, db_column="bankod")  
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
    underlag    = models.CharField(max_length=1, blank=True, db_column="underlag")   

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

    class Meta:
        db_table = "resultat"
        unique_together = ("datum", "bankod", "lopp", "nr")
        ordering = ("datum", "bankod", "lopp", "placering")

    def __str__(self):
        return f"{self.datum} L{self.lopp} #{self.nr} {self.namn}"


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
    
    # //Changed! NY MODELL: Proposition
class Proposition(models.Model):  # //Changed!
    id          = models.BigAutoField(primary_key=True)  # //Changed!
    startdatum  = models.IntegerField(db_column="startdatum")       # //Changed!
    bankod      = models.CharField(max_length=2, db_column="bankod")# //Changed!
    namn        = models.CharField(max_length=50, db_column="namn") # //Changed!
    proposition = models.IntegerField(db_column="proposition")      # //Changed!

    class Meta:  # //Changed!
        db_table = "proposition"                                    # //Changed!
        ordering = ("startdatum", "bankod", "proposition", "namn")  # //Changed!
        unique_together = ("startdatum", "bankod", "namn", "proposition")  # //Changed!

    def __str__(self):  # //Changed!
        return f"{self.startdatum} {self.bankod} {self.proposition} {self.namn}"  # //Changed!
