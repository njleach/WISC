# -*- coding: utf-8 -*-
"""
Created on Mon Jun  4 20:48:26 2018

@author: elcok
"""

import os
import sys
import numpy as np
import pandas as pd
import geopandas as gpd
import subprocess
import rasterio as rio
from rasterio.mask import mask
from shapely.geometry import mapping
from osgeo import ogr
from shapely.wkb import loads
from rasterstats import point_query
from itertools import product
from scipy import ndimage

sys.path.append(os.path.join( '..'))
from scripts.utils import load_config,get_num,int2date

from sklearn import metrics
from tqdm import tqdm

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning) 
pd.set_option('chained_assignment',None)

def region_exposure(region,include_storms=True,event_set=False,sens_analysis_storms=[],save=True):
    """Create a GeoDataframe with exposure and hazard information for each building in the specified region.
    
    Arguments:
        *region* (string) -- NUTS3 code of region to consider.
        
        *include_storms* (bool) -- if set to False, it will only return a list of buildings and their characteristics (default: **True**)
        
        *event_set* (bool) -- if set to True, we will calculate the exposure for the event set instead of the historical storms (default: **True**)
        
        *sens_analysis_storms* (list) -- if empty, it will fill with default list
        
        *save* (bool) -- boolean to decide whether you want to save the output to a csv file (default: **True**)
   
    Returns:
        *GeoDataFrame* with all **hazard** and **exposure** values.
    """    
    country = region[:2]
    
    data_path = load_config()['paths']['data']    
   
    osm_path = os.path.join(data_path,'OSM','{}.osm.pbf'.format(country))
    
    area_poly = os.path.join(data_path,country,'NUTS3_POLY','{}.poly'.format(region))
    area_pbf = os.path.join(data_path,country,'NUTS3_OSM','{}.osm.pbf'.format(region))

#    if (region == 'UKN01') | (region == 'UKN02') | (region == 'UKN03') | (region == 'UKN04') | (region == 'UKN05'):
    if region in ['UKN0{}'.format(x) for x in ['A','B','C','D','E','F','G','6','7','8','9']]:
        osm_path = os.path.join(data_path,'OSM','IE.osm.pbf') 
    
    clip_osm(data_path,osm_path,area_poly,area_pbf)   
    
    gdf_table = fetch_buildings(data_path,country,region,regional=True)
    
    print ('Fetched all buildings from osm data for {}'.format(region))

    # convert to european coordinate system for overlap
    gdf_table = gdf_table.to_crs(epsg=3035)
    
    print(len(gdf_table))

    # Specify Country
    gdf_table["COUNTRY"] = country
    
    # give unique_id 
    gdf_table['ID_'] = [str(x)+'_'+region for x in gdf_table.index]
    
    # Calculate area
    gdf_table["AREA_m2"] = gdf_table.geometry.area

    # Determine centroid
    gdf_table["centroid"] = gdf_table.geometry.centroid
    gdf_table["centroid_epsg4326"] = gdf_table.geometry.to_crs('EPSG:4326').centroid

    nuts_eu = gpd.read_file(os.path.join(data_path,'input_data','NUTS3_ETRS.shp'))

    nuts_eu.loc[nuts_eu['NUTS_ID']==region].to_file(os.path.join(data_path,
                                country,'NUTS3_SHAPE','{}.shp'.format(region)))

    # create geometry envelope outline for rasterstats. Use a buffer to make sure all buildings are in there.
    geoms = [mapping(nuts_eu.loc[nuts_eu['NUTS_ID']==region].geometry.envelope.buffer(10000).values[0])]

    # Get land use values 
    with rio.open(os.path.join(data_path,'input_data','g100_clc12_V18_5.tif')) as src:
        out_image, out_transform = mask(src, geoms, crop=True)
        out_image = out_image[0,:,:]
        tqdm.pandas(desc='CLC_2012_'+region)
        gdf_table['CLC_2012'] = gdf_table.centroid.progress_apply(lambda x: get_raster_value(x,out_image,out_transform))

    # Obtain storm values for sensitivity analysis storms   
    if len(sens_analysis_storms) > 0:
        storm_list = load_sens_analysis_storms(sens_analysis_storms)
        for outrast_storm in storm_list:
            storm_name = str(int2date(get_num(outrast_storm[-23:].split('_')[0][:-2])))
            tqdm.pandas(desc=storm_name+'_'+region)
            with rio.open(outrast_storm) as src:
                out_image, out_transform = mask(src, geoms, crop=True)
                out_image = out_image[0,:,:]
                gdf_table[storm_name] = gdf_table.centroid.progress_apply(lambda x: get_raster_value(x,out_image,out_transform))

    # Obtain storm values for historical storms
    elif (include_storms == True) & (event_set == False):
        storm_list = get_storm_list(data_path)
        for outrast_storm in storm_list:
            storm_name = str(int2date(get_num(outrast_storm[-23:].split('_')[0][:-2])))
            tqdm.pandas(desc=storm_name+'_'+region)
            with rio.open(outrast_storm) as src:
                out_image, out_transform = mask(src, geoms, crop=True)
                out_image = out_image[0,:,:]
                gdf_table[storm_name] = gdf_table.centroid.progress_apply(lambda x: get_raster_value(x,out_image,out_transform))
                gdf_table[storm_name].loc[gdf_table[storm_name] < 0] = 0 
                gdf_table[storm_name].loc[gdf_table[storm_name] > 500] = 0 

    # Obtain storm values for event set storms
    elif (include_storms == True) & (event_set == True):
        #geoms = [mapping(nuts_eu.loc[nuts_eu['NUTS_ID']==region].to_crs({'init': 'epsg:4326'}).geometry.envelope.buffer(0.1).values[0])]
        storm_list = get_event_storm_list(data_path)[:]
        for outrast_storm in tqdm(storm_list,total=len(storm_list),desc=region):
#            storm_name = str(int2date(get_num(outrast_storm[-24:].split('_')[0][:-4])))
            storm_name = outrast_storm.split('/')[-1].split('.')[0]
            with rio.open(outrast_storm) as src:
                out_image = src.read(1)
                out_transform = src.transform
                gdf_table[storm_name] = gdf_table.centroid_epsg4326.apply(lambda x: get_raster_value(x,out_image,out_transform))

    if save == True:
        df_exposure = pd.DataFrame(gdf_table)
        df_exposure.to_csv(os.path.join(data_path,'output_exposure',country,'{}_exposure.csv'.format(region)))        

    print ('Obtained all storm information for {}'.format(region))

    return gdf_table        


def region_losses(region,storm_event_set=False,sample=(5, 0,95,20,80)):
    """Estimation of the losses for all buildings in a region for a pre-defined list of storms.
    
    Arguments:
        *region* (string) -- nuts code of region to consider.
        
        *storm_event_set* (bool) -- calculates all regions within a country parallel. Set to **False** if you have little capacity on the machine (default: **True**).
        
        *sample* (tuple) -- tuple of parameter values. This is a dummy placeholder, should be filled with either **load_sample(country)** values or **sens_analysis_param_list**.
    
    Returns:
        *pandas Dataframe* -- pandas dataframe with all buildings of the region and their **losses** for each wind storm.

    """ 
    data_path = load_config()['paths']['data']    
    
    country = region[:2]

    #load storms
    if storm_event_set == False:
        storm_list = get_storm_list(data_path)
        storm_name_list = [str(int2date(get_num(x[-23:].split('_')[0][:-2]))) for x in storm_list]

    else:
        storm_list = get_event_storm_list(data_path)
        storm_name_list = [x.split('/')[-1].split('.')[0] for x in storm_list]
#        storm_name_list = [str(int2date(get_num(x[-24:].split('_')[0][:-4]))) for x in storm_list]

    #load max dam
    max_dam = load_max_dam(data_path)
  
    #load curves
    curves = load_curves(data_path)
   
    output_table = region_exposure(region,include_storms=True,event_set=storm_event_set)
    if output_table.empty:
        print('No building info returned, exiting region')
        return None

    no_storm_columns = list(set(output_table.columns).difference(list(storm_name_list)))
    write_output = pd.DataFrame(output_table[no_storm_columns])

    ## Calculate losses for buildings in this NUTS region
    for storm in storm_name_list:
        write_output[storm] = loss_calculation(storm,country,output_table,max_dam,curves,sample)
    
    df_losses = pd.DataFrame(write_output)

    ## save this regional file
    if storm_event_set == False:
        df_losses.to_csv(os.path.join(data_path,'output_losses',country,'{}_losses.csv'.format(region)))
 
        print ('Finished with loss calculation for {}'.format(region))
    
        return(gpd.GeoDataFrame(write_output))
        
    else:
        #Numpify data
        pdZ = np.array(df_losses[storm_name_list],dtype=int)
        write_output.drop(storm_name_list, axis=1, inplace=True)
       
        output_ =[]
        
        for row in pdZ:
            H,X1 = np.histogram(row, bins = 100, normed = True )
            dx = X1[1] - X1[0]
            F1 = np.cumsum(np.append(0,H))*dx
            output_.append(metrics.auc(X1, F1))
        
        df_losses['Risk'] = output_
        
        df_losses.to_csv(os.path.join(data_path,'output_risk',country,'{}_risk.csv'.format(region)))

        print ('Finished with risk calculation for {}'.format(region))
        
        return(gpd.GeoDataFrame(write_output))
    

def region_sens_analysis(region,samples,sens_analysis_storms=[],save=True):
    """Perform a sensitivity analysis for the specified region, based on a predefined list of storms. 
    
    Arguments:
        *region* (string) -- nuts code of region to consider.

        *samples* (list) -- list o tuples, where each tuple is a **unique** set of parameter values.
    
        *sens_analysis_storms* (list) -- if empty, it will fill with default list

        *save* (bool) -- boolean to decide whether you want to save the output to a csv file (default: **True**)

    Returns:
        *list* -- list with the total losses per storm for all parameter combinations

    """ 
    
    data_path = load_config()['paths']['data']    
    
    country = region[:2]

    # select storms to assess
    if len(sens_analysis_storms) == 0:
        sens_analysis_storms = ['19991203','19900125','20090124','20070118','19991226']

    storm_list = sens_analysis_storms

    all_combis = list(product(samples,storm_list))

    #load max dam
    max_dam = load_max_dam(data_path)
  
    #load curves
    curves = load_curves(data_path)

    # get exposure table   
    output_table = region_exposure(region,include_storms=True,event_set=False,sens_analysis_storms=storm_list,save=True)
    
    # calculate losses for all combinations
    output_file = pd.DataFrame(index=list(range(len(samples))),columns=sens_analysis_storms)
    for iter_,(sample,storm) in enumerate(all_combis):
        output_file.loc[iter_,storm] = list(loss_calculation(storm,country,output_table,max_dam,curves,sample).sum())[0]
    
    if save == True:
        output_file.to_csv(os.path.join(data_path,'output_sens','{}_sens_analysis'.format(region)))
    
    return(output_file)

def loss_calculation(storm,country,output_table,max_dam,curves,sample):
    """Calculate the losses per storm.
    
    Arguments:
        *storm* (string) -- date of the storm.
        
        *region* (string) -- NUTS3 code of region to consider.
        
        *output_table* (GeoDataFrame) -- GeoDataFrame with all buildings and the wind speed values for each storm.
        
        *max_dam* (numpy array) -- table with maximum damages per building type/land-use class.
        
        *curves*  (pandas dataframe) -- fragility curves for the different building types.
        
        *sample* (list) -- ratios of different curves used in this study. See the **Sensitivity analysis documentation** for an explanation.
   
    Returns:
        *pandas Series* -- losses to all buildings for the specified storm
    """   
    
    max_dam_country = np.asarray(max_dam[max_dam['CODE'].str.contains(country)].iloc[:,1:],dtype='int16')    

    df_C2 = pd.DataFrame(output_table[['AREA_m2','CLC_2012',storm]])
    df_C3 = pd.DataFrame(output_table[['AREA_m2','CLC_2012',storm]])
    df_C4 = pd.DataFrame(output_table[['AREA_m2','CLC_2012',storm]])
 
    # error processing of large integer value in storm data. 
    df_C2_error = df_C2[df_C2[storm].astype(str).str.split(".").str[0].map(len) > 10 ]
    df_C3_error = df_C3[df_C3[storm].astype(str).str.split(".").str[0].map(len) > 10 ]
    df_C4_error = df_C4[df_C4[storm].astype(str).str.split(".").str[0].map(len) > 10 ]
    
    if not df_C2_error.empty:
        print(df_C2_error)
    if not df_C3_error.empty:
        print(df_C3_error)
    if not df_C4_error.empty:
        print(df_C4_error)

    df_C2[str(storm)+'_curve'] = df_C2[storm].astype('int64').map(curves['C2']) 
    df_C3[str(storm)+'_curve'] = df_C3[storm].astype('int64').map(curves['C3'])
    df_C4[str(storm)+'_curve'] = df_C4[storm].astype('int64').map(curves['C4']) 

    # curves are in percentages, rescale to ratio
    df_C2[str(storm)+'_curve'] = df_C2[str(storm)+'_curve']/100
    df_C3[str(storm)+'_curve'] = df_C3[str(storm)+'_curve']/100
    df_C4[str(storm)+'_curve'] = df_C4[str(storm)+'_curve']/100
 
    #specify shares for urban and nonurban        
    RES_URB = sample[4]/100 
    IND_URB = sample[3]/100   

    RES_NONURB = 0.5
    IND_NONURB = 0.5

    # Use pandas where to fill new column for losses
    df_C2['Loss'] = np.where(df_C2['CLC_2012'].between(0,12, inclusive=True), (df_C2['AREA_m2']*df_C2[str(storm)+'_curve']*max_dam_country[0,0]*RES_URB+df_C2['AREA_m2']*df_C2[str(storm)+'_curve']*max_dam_country[0,2]*IND_URB)*(sample[0]/100), 0)
    df_C2['Loss'] = np.where(df_C2['CLC_2012'].between(13,23, inclusive=True), (df_C2['AREA_m2']*df_C2[str(storm)+'_curve']*max_dam_country[0,0]*RES_NONURB+df_C2['AREA_m2']*df_C2[str(storm)+'_curve']*max_dam_country[0,2]*IND_NONURB)*(sample[0]/100),df_C2['Loss'])

    df_C3['Loss'] = np.where(df_C3['CLC_2012'].between(0,12, inclusive=True), (df_C3['AREA_m2']*df_C3[str(storm)+'_curve']*max_dam_country[0,0]*RES_URB+df_C3['AREA_m2']*df_C3[str(storm)+'_curve']*max_dam_country[0,2]*IND_URB)*(sample[1]/100), 0)
    df_C3['Loss'] = np.where(df_C3['CLC_2012'].between(13,23, inclusive=True), (df_C3['AREA_m2']*df_C3[str(storm)+'_curve']*max_dam_country[0,0]*RES_NONURB+df_C3['AREA_m2']*df_C3[str(storm)+'_curve']*max_dam_country[0,2]*IND_NONURB)*(sample[1]/100),df_C3['Loss'])

    df_C4['Loss'] = np.where(df_C4['CLC_2012'].between(0,12, inclusive=True), (df_C4['AREA_m2']*df_C4[str(storm)+'_curve']*max_dam_country[0,0]*RES_URB+df_C4['AREA_m2']*df_C4[str(storm)+'_curve']*max_dam_country[0,2]*IND_URB)*(sample[2]/100), 0)
    df_C4['Loss'] = np.where(df_C4['CLC_2012'].between(13,23, inclusive=True), (df_C4['AREA_m2']*df_C4[str(storm)+'_curve']*max_dam_country[0,0]*RES_NONURB+df_C4['AREA_m2']*df_C4[str(storm)+'_curve']*max_dam_country[0,2]*IND_NONURB)*(sample[2]/100),df_C4['Loss'])

    # and write output 
    return  (df_C2['Loss'].fillna(0).astype(int) + df_C3['Loss'].fillna(0).astype(int) + df_C4['Loss'].fillna(0).astype(int))        

def get_storm_data(storm_path):
    """  Obtain raster grid of the storm with rasterio
    
    Arguments:
        *storm_path* (string) -- path to location of storm
    
    """
    with rio.open(storm_path) as src:    
        # Read as numpy array
        array = src.read(1)
        array = np.array(array,dtype='float32')
        affine_storm = src.affine
    return array,affine_storm

def extract_buildings(area,country,NUTS3=True):
    """Extracts building from OpenStreetMap pbf file and saves it to an ESRI shapefile
    
    Arguments:
        *area* (string) -- name of area to clip
        
        *country* (string) -- ISO2 code of country to consider.
    
        *NUTS3* (bool) -- specify whether it will be a clip of NUTS3 region or the whole country (default: **True**)
    
    """
    # get data path
    data_path = load_config()['paths']['data']

    wgs = os.path.join(data_path,country,'NUTS3_BUILDINGS','{}_buildings.shp'.format(area))
    if NUTS3 == True:
        pbf = os.path.join(data_path,country,'NUTS3_OSM','{}.osm.pbf'.format(area))
    else:
        pbf = os.path.join(data_path,'OSM','{}.osm.pbf'.format(area))
  
    os.system('ogr2ogr -progress -f "ESRI shapefile" {} {} -sql "select \
              building,amenity from multipolygons where building is not null" \
              -lco ENCODING=UTF-8 -nlt POLYGON -skipfailures'.format(wgs,pbf))

def convert_buildings(area,country):
    """Converts the coordinate system from EPSG:4326 to EPSG:3035.

    Arguments:
        *area* (string) -- name of area (most often NUTS3) for which buildings should be converted to European coordinate system.

        *country* (string) -- ISO2 code of country to consider.

    Returns:
        *GeoDataframe* -- Geopandas dataframe with all buildings of the selected area
        
    """
    # get data path
    data_path = load_config()['paths']['data']

    # path to area with buildings
    etrs = os.path.join(data_path,country,'NUTS3_BUILDINGS','{}_buildings.shp'.format(area))

    # load data 
    input_ = gpd.read_file(etrs)

    input_ = input_.to_crs(epsg=3035)

    return input_
    
def get_storm_list(data_path):
    """Small function to create a list of with path strings to all storms.
    
    Arguments:
        *data_path* (string) -- string of data path where all data is located.

    Returns:
        *list* -- list with the path strings of all storms
    """
   
    storm_list = []
    for root, dirs, files in os.walk(os.path.join(data_path,'STORMS')):
        for file in files:
            if file.endswith('.tif'):
                fname = os.path.join(data_path,'STORMS',file)
                (filepath, filename_storm) = os.path.split(fname) 
                (fileshortname_storm, extension) = os.path.splitext(filename_storm) 
                resample_storm =  os.path.join(data_path,'STORMS',fileshortname_storm+'.tif')
                storm_list.append(resample_storm)    

    return storm_list

def get_event_storm_list(data_path):
    """Small function to create a list of with path strings to all storms in the event set.
    
    Arguments:
        *data_path* (string) -- string of data path where all data is located.

    Returns:
        *list* -- list with the path strings of all storms    
    """
    storm_list = []
    for root, dirs, files in os.walk(os.path.join(data_path,'event_set_tif')):
        for file in files:
            if file.endswith('.tif'):
                fname = os.path.join(data_path,'event_set_tif',file)
                (filepath, filename_storm) = os.path.split(fname) 
                (fileshortname_storm, extension) = os.path.splitext(filename_storm) 
                resample_storm =  os.path.join(data_path,'event_set_tif',fileshortname_storm+'.tif')
                storm_list.append(resample_storm)    

    return storm_list    

def load_max_dam(data_path):
    """Small function to load the excel with maximum damages.
    
    Arguments:
        *data_path* (string) -- string of data path where all data is located.

    Returns:
        *dataframe* -- pandas dataframe with maximum damages per landuse.
    """

    return pd.read_csv(os.path.join(data_path,'input_data','max_dam2.csv'))


def load_curves(data_path):
    """Small function to load the csv file with the different fragility curves.
    
    Arguments:
        *data_path* (string) -- string of data path where all data is located.

    Returns:
        *dataframe* -- pandas dataframe with fragility curves
    """

    return pd.read_csv(os.path.join(data_path,'input_data','CURVES.csv'),index_col=[0],names=['C1','C2','C3','C4','C5','C6'])


def load_sample(country):
    """Will load the ratio of each curve and landuse to be used.
    
    Arguments:
        *country* (string) -- ISO2 code of country to consider.

    Returns:
        *tuple* -- tuple with ratios for the selected country. See the **documentation** for an explanation.
        
        **['c2', 'c3', 'c4','lu1','lu2']**
        
    """

    dict_  = dict([('AT', ( 5, 0,95,20,80)),
                   ('CZ', ( 5, 0,95,20,80)),
                   ('CH', ( 5, 0,95,20,80)), 
                         ('BE', ( 0,45,55,50,50)), 
                         ('DK', ( 0,10,90,20,80)),
                         ('FR', (10,30,60,20,80)), 
                         ('DE', ( 5,75,20,50,50)),
                         ('IE', (35,65, 0,30,70)), 
                         ('LU', (50,50, 0,20,80)),
                         ('NL', ( 0,45,55,20,80)), 
                         ('NO', (0,100, 0,20,80)),
                         ('FI', (5,15, 80,50,50)),
                         ('LT', (5,15, 80,50,50)),
                         ('LV', (5,15, 80,50,50)),
                         ('EE', (5,15, 80,50,50)),
                         ('PL', (5,15, 80,30,70)),
                         ('IT', (10,90, 0,20,80)),
                         ('ES', (15,85, 0,20,80)),
                         ('PT', (15,85, 0,20,80)),
                         ('SE', ( 0,10,90,30,70)),
                         ('UK', ( 5,30,65,50,50))])

    return dict_[country]

def load_osm_data(data_path,country,region='',regional=False):
    """This function loads the OSM file for the country
    
    Arguments:
        *data_path* (string) -- string of data path where all data is located.
        
        *country* (string) -- ISO2 code of country to consider.
        
        *region* (string) -- NUTS3 code of region to consider.
        
        *regional* (boolean) -- Boolean to decide whether this should be done on a regional level or for the entire country (default: **False**). 

    Returns:
        opened OSM file to use in the fetch_roads function.
    
    """
    
    if regional==False:
        osm_path = os.path.join(data_path,'OSM','{}.osm.pbf'.format(country))
    else:
        osm_path = os.path.join(data_path,country,'NUTS3_OSM','{}.osm.pbf'.format(region))
        

    driver=ogr.GetDriverByName('OSM')
    return driver.Open(osm_path)

def fetch_buildings(data_path,country,region='',regional=False):
    """
    This function directly reads the building data from osm, instead of first converting it to a shapefile
    
    Arguments:
        *data_path* (string) -- string of data path where all data is located.
        
        *country* (string) -- ISO2 code of country to consider.
        
        *region* (string) -- NUTS3 code of region to consider.
        
        *regional* (boolean) -- Boolean to decide whether this should be done on a regional level or for the entire country (default: **False**). 
     
    Returns:
        *Geodataframe* -- Geopandas dataframe with all buildings in the specified **region** or **country**.
     
    """
    data = load_osm_data(data_path,country,region,regional=regional)
    roads=[]    
    if data is not None:
        sql_lyr = data.ExecuteSQL("SELECT osm_id,building from multipolygons where building is not null")
        for feature in sql_lyr:
            try:
                if feature.GetField('building') is not None:
                    osm_id = feature.GetField('osm_id')
                    shapely_geo = loads(feature.geometry().ExportToWkb())
                    if shapely_geo is None:
                        continue
                    highway=feature.GetField('building')
                    #amenity=feature.GetField('amenity')
                    roads.append([osm_id,highway,shapely_geo])
            except:
                    print("warning: skipped building")
        print(len(roads))
    else:
        print("Nonetype error when requesting SQL for region: {}. Skipping region, check required.".format(region))    

    if len(roads) > 0:
        return gpd.GeoDataFrame(roads,columns=['osm_id','building','geometry'],crs={'init': 'epsg:4326'})
    else:
        # raise Exception('No buildings in {}'.format(region))
        print("warning: No buildings or No memory when running region: {}. returning empty geodataframe".format(region)) 
        return gpd.GeoDataFrame(columns=['osm_id','building','geometry'],crs={'init': 'epsg:4326'})
    
def poly_files(data_path,country):

    """
    This function will create the .poly files from the nuts shapefile. These
    .poly files are used to extract data from the openstreetmap files.
    
    This function is adapted from the OSMPoly function in QGIS.
    
    Arguments:
        *data_path* (string) -- string of data path where all data is located.
        
        *country* (string) -- ISO2 code of country to consider.
   
    Returns:
        *.poly file* -- .poly file for each NUTS3 in a new directory in the working directory of the country.
    """     
   
# =============================================================================
#     """ Create output dir for .poly files if it is doesnt exist yet"""
# =============================================================================
    poly_dir = os.path.join(data_path,country,'NUTS3_POLY')
        
    if not os.path.exists(poly_dir):
        os.makedirs(poly_dir)

# =============================================================================
#   """Load country shapes and country list and only keep the required countries"""
# =============================================================================
    wb_poly = gpd.read_file(os.path.join(data_path,'input_data','NUTS3_ETRS.shp'))
    
    # filter polygon file
    country_poly = wb_poly.loc[(wb_poly['NUTS_ID'].apply(lambda x: x.startswith(country))) & (wb_poly['LEVL_CODE']==3)]

    country_poly.crs = {'init' :'epsg:3035'}

    country_poly = country_poly.to_crs({'init': 'epsg:4326'})
    
# =============================================================================
#   """ The important part of this function: create .poly files to clip the country 
#   data from the openstreetmap file """    
# =============================================================================
    num = 0
    # iterate over the counties (rows) in the world shapefile
    for f in country_poly.iterrows():
        f = f[1]
        num = num + 1
        geom=f.geometry

#        try:
        # this will create a list of the different subpolygons
        if geom.geom_type == 'MultiPolygon':
            polygons = geom
        
        # the list will be lenght 1 if it is just one polygon
        elif geom.geom_type == 'Polygon':
            polygons = [geom]

        # define the name of the output file, based on the ISO3 code
        attr=f['NUTS_ID']
        
        # start writing the .poly file
        f = open(poly_dir + "/" + attr +'.poly', 'w')
        f.write(attr + "\n")

        i = 0
        
        # loop over the different polygons, get their exterior and write the 
        # coordinates of the ring to the .poly file
        for polygon in polygons:
            polygon = np.array(polygon.exterior)

            j = 0
            f.write(str(i) + "\n")

            for ring in polygon:
                j = j + 1
                f.write("    " + str(ring[0]) + "     " + str(ring[1]) +"\n")

            i = i + 1
            # close the ring of one subpolygon if done
            f.write("END" +"\n")

        # close the file when done
        f.write("END" +"\n")
        f.close()

def clip_landuse(data_path,country,region,outrast_lu):
    """Clip the landuse from the European Corine Land Cover (CLC) map to the considered country.
    
    Arguments:
        *data_path* (string) -- string of data path where all data is located.
        
        *country* (string) -- ISO2 code of country to consider.
        
        *outrast_lu* (string) -- string path to location of Corine Land Cover dataset 
        
    Returns:
        *raster* (geotiff) -- returns a raster file with only the landuse of the clipped **region** or **country**.
        
    """
    
    inraster = os.path.join(data_path,'input_data','g100_clc12_V18_5.tif')
    
    inshape = os.path.join(data_path,country,'NUTS3_SHAPE','{}.shp'.format(region))
   
    subprocess.call(["gdalwarp","-q","-overwrite","-srcnodata","-9999","-co","compress=lzw","-tr","100","-100","-r","near",inraster, outrast_lu, "-cutline", inshape,"-crop_to_cutline"])
 

def clip_osm(data_path,osm_path,area_poly,area_pbf):
    """ Clip the an area osm file from the larger continent (or planet) file and save to a new osm.pbf file. 
    
    This is much faster compared to clipping the osm.pbf file while extracting through ogr2ogr.
    
    This function uses the osmconvert tool, which can be found at http://wiki.openstreetmap.org/wiki/Osmconvert. 
    
    Either add the directory where this executable is located to your environmental variables or just put it in the 'scripts' directory.
    
    Arguments:
        *osm_path* (string) -- path string to the osm.pbf file of the continent associated with the country.
        
        *area_pol* (string) -- path string to the .poly file, made through the 'create_poly_files' function.
        
        *area_pbf* (string) -- path string indicating the final output dir and output name of the new .osm.pbf file.
        
    Returns:
        a clipped .osm.pbf file.
    """ 

    osm_convert_path = os.path.join(data_path,'osmconvert','osmconvert64')
    try: 
        if (os.path.exists(area_pbf) is not True):
            os.system('{}  {} -B={} --complete-ways -o={}'.format(osm_convert_path,osm_path,area_poly,area_pbf))

    except:
        print('{} did not finish!'.format(area_pbf))
        
def load_sens_analysis_storms(storm_name_list=['19991203','19900125','20090124','20070118','19991226']):
    """
    This file load the storms used to perform the sensitivity analysis. 
    
    Arguments:
        *storm_name_list* (list) -- list of storms to include in the sensitivity analysis. The default storms are **Anatol**, **Daria**, **Klaus**, **Kyrill** and **Lothar**.
    
    Returns:
        *storm_list* (list) -- same list of storms but now with full paths to location in directory.
    
    """
    data_path = load_config()['paths']['data']

    storm_list = []
    for root, dirs, files in os.walk(os.path.join(data_path,'STORMS')):
        for file in files:
            for storm in storm_name_list:
                if storm in file:
                    storm_list.append(os.path.join(data_path,'STORMS',file))
    return storm_list

def get_raster_value(centroid,out_image,out_transform):
    """Small function to obtain raster value from rastermap, using point_query.
    
    Arguments:
        *centroid* (shapely geometry) -- shapely geometry of centroid of the building.
        
        *out_image* (numpy array) -- numpy array of grid.
        
        *out_transform* (Affine) -- georeference of numpy array.
        
    Returns:
        *integer* -- raster value corresponding to location of centroid
    """

    return int(point_query(centroid,out_image,affine=out_transform,nodata=-9999,interpolate='nearest')[0] or 255)   

                    
def summary_statistics_losses():
    """
    This function creates the file 'output_storms.xlsx'. This file is required to create the summary figures.
    
    Returns:
        *output_storms.xlsx* (excel file) -- Excel file with summary outcomes
    """
    
    data_path = load_config()['paths']['data']
    
    countries = ['AT','BE','DK','FR','DE','IE','LU','NL','NO','SE','UK','PL','IT','FI'] 

    first_line = pd.read_csv(os.path.join(data_path,'output_losses','LU','LU000_losses.csv'), nrows=1)
    extract = first_line.columns.tolist()[2:]
    storm_name_list = extract[8:]
    
    output_storms = pd.DataFrame(np.zeros((len(storm_name_list),len(countries))),index=storm_name_list,columns=countries)
    output_storms_res = pd.DataFrame(np.zeros((len(storm_name_list),len(countries))),index=storm_name_list,columns=countries)
    output_storms_ind_com = pd.DataFrame(np.zeros((len(storm_name_list),len(countries))),index=storm_name_list,columns=countries)

    output_storms_transport = pd.DataFrame(np.zeros((len(storm_name_list),len(countries))),index=storm_name_list,columns=countries)
    output_storms_other = pd.DataFrame(np.zeros((len(storm_name_list),len(countries))),index=storm_name_list,columns=countries)
    output_storms_agri = pd.DataFrame(np.zeros((len(storm_name_list),len(countries))),index=storm_name_list,columns=countries)

    for country in countries:
       output_table = pd.DataFrame()
       for root, dirs, files in os.walk(os.path.join(data_path,'output_losses',country)):
           for file in files:
                output_table = pd.read_csv(os.path.join(data_path,'output_losses',country,file), usecols=extract)
                output_table = output_table.replace([np.inf, -np.inf], np.nan).dropna(how='all')
                output_table = output_table.reset_index(inplace=False)
                # TOTAL
                output_storms[country] += (output_table[storm_name_list].sum(axis=0)/1000000)
                # RESIDENTIAL
                res = output_table[output_table.CLC_2012 < 3]
                output_storms_res[country] += (res[storm_name_list].sum(axis=0)/1000000)     
                # COM/IND
                ind_com = output_table[output_table.CLC_2012 == 3]
                output_storms_ind_com[country] += (ind_com[storm_name_list].sum(axis=0)/1000000)
                # TRANSPORT,PORTS,AIRPORTS
                transport = output_table.loc[np.where(output_table['CLC_2012'].between(4,6, inclusive=True))[0]]
                output_storms_transport[country] += (transport[storm_name_list].sum(axis=0)/1000000)
                # OTHER BUILT-UP
                other = output_table.loc[np.where(output_table['CLC_2012'].between(7,12, inclusive=True))[0]]
                output_storms_other[country] += (other[storm_name_list].sum(axis=0)/1000000)
                # AGRICULTURAL BUILDINGS
                agri = output_table[output_table.CLC_2012 > 12]
                output_storms_agri[country] += (agri[storm_name_list].sum(axis=0)/1000000)
    
    output_storms['Sum'] = output_storms.sum(axis=1)
    output_storms_res['Sum'] = output_storms_res.sum(axis=1)
    output_storms_ind_com['Sum'] = output_storms_ind_com.sum(axis=1)
    output_storms_transport['Sum'] = output_storms_transport.sum(axis=1)
    output_storms_other['Sum'] = output_storms_other.sum(axis=1)
    output_storms_agri['Sum'] = output_storms_agri.sum(axis=1)

    out = pd.ExcelWriter(os.path.join(data_path,'output_storms.xlsx'))
    output_storms.to_excel(out,sheet_name='total_losses',index_label='Storm')
    output_storms_res.to_excel(out,sheet_name='res_losses',index_label='Storm')
    output_storms_ind_com.to_excel(out,sheet_name='ind_com_losses',index_label='Storm')
    output_storms_transport.to_excel(out,sheet_name='transport_losses',index_label='Storm')
    output_storms_other.to_excel(out,sheet_name='other_losses',index_label='Storm')
    output_storms_agri.to_excel(out,sheet_name='agri_losses',index_label='Storm')
    out.save()
