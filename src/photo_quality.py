"""Conservative in-situ Wikimedia Commons photo selection for the Red Sea field guide."""
import re
from PIL import Image, ImageStat, ImageFilter

BAD_CONTEXT = re.compile(
    r'\b(?:aquarium|aquaria|zoological garden|zoo|museum|specimen|taxiderm(?:y|ied)?|'
    r'preserved|collection tray|cabinet|dried|mounted|display case|scientific illustration|'
    r'illustration|drawing|painting|watercolou?r|vector|sketch|plate from|'
    r'bleach(?:ed|ing)?|dead coral|dead reef|coral skeleton|coral rubble|'
    r'fossili[sz]ed|washed ashore|dried shell|distribution map|range map|'
    r'graphic|line art|cutout|clipart|postcard|poster|stamp|diagram)\b', re.I)
SUSPECT_FILENAME = re.compile(
    r'\b(?:aquarium|museum|specimen|taxidermy|dead|bleached|skeleton|'
    r'collection|illustration|drawing|painting|sketch|map|poster|stamp|'
    r'silhouette|zoo|fossil|preserved|mounted|graphic|plate)\b', re.I)
POSITIVE_CONTEXT = (
    'ras katy', 'ras um sid', 'ras mohammed', 'ras muhammad', 'sharm',
    'sinai', 'egypt', 'egipto', 'mar rojo', 'red sea', 'eilat', 'aqaba',
    'underwater', 'scuba', 'snorkel', 'coral reef', 'reef', 'wild',
    'natural habitat', 'diving')

def bad_metadata(filename, description):
    """Reject explicitly captive, preserved, skeletal, or illustrated subjects."""
    return bool(SUSPECT_FILENAME.search(filename) or BAD_CONTEXT.search(description))

def metadata_bonus(value):
    t=value.casefold()
    score=0.0
    for word in POSITIVE_CONTEXT:
        if word in t:
            score += 4 if word in ('reef','underwater','natural habitat') else 6
    return min(45.0,score)

def image_metrics(im):
    small=im.convert('RGB').resize((92,92),Image.Resampling.BILINEAR)
    hsv=list(small.convert('HSV').getdata())
    rgb=list(small.getdata())
    n=len(rgb)
    saturation=sum(p[1] for p in hsv)/(255*n)
    underwater=sum(1 for r,g,b in rgb if b>=r*1.17 and g>=r*1.07 and 22<b<247)/n
    paper=sum(1 for r,g,b in rgb if r>224 and g>224 and b>224)/n
    black=sum(1 for r,g,b in rgb if r<35 and g<35 and b<35)/n
    gray=sum(1 for r,g,b in rgb if max(r,g,b)-min(r,g,b)<23)/n
    contrast=ImageStat.Stat(small.convert('L')).stddev[0]
    edges=ImageStat.Stat(small.convert('L').filter(ImageFilter.FIND_EDGES)).stddev[0]
    return dict(saturation=round(saturation,3),blue=round(underwater,3),
        paper=round(paper,3),black=round(black,3),gray=round(gray,3),
        contrast=round(contrast,1),edges=round(edges,1),
        width=im.width,height=im.height)

def photo_score(im,section,info_text=''):
    """Score actual pixels. Not an image-classifier; species identity stays metadata-bound."""
    m=image_metrics(im)
    if m['paper']>0.34 and m['blue']<0.12: return -100.0,m
    if m['black']>0.82 and m['blue']<0.06: return -100.0,m
    if m['saturation']<0.075 and m['blue']<0.09: return -100.0,m
    if min(im.size)<300: return -100.0,m
    score=17.0+min(37.0,m['saturation']*80)
    score+=min(20.0,m['blue']*46)+min(12.0,m['contrast']*.17)
    score+=min(8.0,m['edges']*.35)-m['paper']*65-m['gray']*16
    score-=max(0,m['black']-.38)*30
    if section=='coral':score+=m['saturation']*15
    if m['blue']>.12 and m['saturation']>.24:score+=10
    if min(im.size)>=650:score+=5
    return round(score,2),m

def ahash(im):
    px=list(im.convert('L').resize((10,10),Image.Resampling.BILINEAR).getdata())
    mean=sum(px)/len(px);result=0
    for i,value in enumerate(px):
        if value>mean:result|=(1<<i)
    return result

def similar(a,b):
    return (a^b).bit_count()<12
