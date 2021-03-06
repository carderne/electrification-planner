from math import sqrt
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString
from pathlib import Path

import sys
sys.path.insert(0, '../minigrid-optimiser')
from mgo import mgo



def load_clusters(clusters_file, grid_dist_connected=1000, minimum_pop=200):
    """

    """
    # Read in the clusters file, convert to desired CRS (ostensibly better for distances) and convert to points, filter on population along the way
    clusters = gpd.read_file(str(clusters_file))
    # This is the Africa Albers Equal Area Conic EPSG: 102022
    epsg102022 = '+proj=aea +lat_1=20 +lat_2=-23 +lat_0=0 +lon_0=25 +x_0=0 +y_0=0 +ellps=WGS84 +datum=WGS84 +units=m +no_defs'
    clusters = clusters.to_crs(epsg102022)

    clusters['conn_start'] = 0
    clusters.loc[clusters['grid_dist'] <= grid_dist_connected, 'conn_start'] = 1
    clusters = clusters.loc[clusters['pop_sum'] > minimum_pop]
    clusters = clusters.sort_values('pop_sum', ascending=False)  # so that biggest (and thus connected) city gets index=0
    clusters = clusters.reset_index().drop(columns=['index'])

    return clusters


def create_network(clusters):
    """
    We then take all the clusters and calculate the optimum network that connects them all together.
    The ML model returns T_x and T_y containing the start and end points of each new arc created
    """

    clusters_points = clusters.copy()
    clusters_points.geometry = clusters_points['geometry'].centroid
    clusters_points['X'] = clusters_points.geometry.x
    clusters_points['Y'] = clusters_points.geometry.y

    df = pd.DataFrame(clusters_points)
    points = df[['X', 'Y']].as_matrix()

    T_x, T_y = mgo.get_spanning_tree(points)

    # This point and line data is then copied into two arrays, called *nodes* and *network*,
    # containing the clusters and lines, respectively. Each element represents a single cluster or joining arc,
    # and has data within describing the coordinates and more.

    # **Structure for nodes**  
    # 0   index  
    # 1   x  
    # 2   y  
    # 3   area_m2  
    # 4   pop_sum  
    # 5   connected  
    # 6   new_conn  
    # 7   off_grid_cost  
    # 8   [connected arc indices]  
    # 
    # **Structure for network**  
    # 0   index  
    # 1   xs  
    # 2   ys  
    # 3   xe  
    # 4   ye  
    # 5   node index first point  
    # 6   node index last point  
    # 7   existing  
    # 8   arc length  
    # 9   whether enabled

    df['conn_end'] = df['conn_start']
    df['off_grid_cost'] = 0

    nodes = df[['X', 'Y', 'area_m2', 'pop_sum', 'conn_start', 'conn_end', 'off_grid_cost']].reset_index().values.astype(int).tolist()

    # add an empty list at position 8 for connected arc indices
    for node in nodes:
        node.append([])

    counter = 0
    network = []
    for xs, ys, xe, ye in zip(T_x[0], T_y[0], T_x[1], T_y[1]):
        xs = int(xs)
        ys = int(ys)
        xe = int(xe)
        ye = int(ye)
        length = int(sqrt((xe - xs)**2 + (ye - ys)**2))
        network.append([counter, xs, ys, xe, ye, None, None, 1, length, 1])
        counter += 1


    network, nodes = connect_network(network, nodes, 0)

    # for every node, add references to every arc that connects to it
    for arc in network:
        nodes[arc[5]][8].append(arc[0])
        nodes[arc[6]][8].append(arc[0])
        
    # set which arcs don't already exist (and the remainder do!)
    for node in nodes:
        if node[5] == 0:
            connected_arcs = [network[arc_index] for arc_index in node[8]]
            for arc in connected_arcs:
                arc[7] = 0
                arc[9] = 0 

    return network, nodes


def connect_network(network, nodes, index):
    """
    Then we need to tell each arc which nodes it is connected to, and likewise for each node
    Each arc connects two nodes, each node can have 1+ arcs connected to it
    """

    cur_node = nodes[index]
    for arc in network:
        found = 0
        if arc[5] == None and arc[6] == None:  # if this arc has no connected nodes
            if (arc[1] == cur_node[1] and arc[2] == cur_node[2]):  # if the xs and ys match a node
                found = 3  # point towards position 3 (xe) for the next node
            if (arc[3] == cur_node[1] and arc[4] == cur_node[2]):  # if the xe and ye match a node
                found = 1  # point towards position 1 (xs) for the next node

            if found:
                arc[5] = cur_node[0] # tell this arc that this node is its starting point
            
                for node in nodes:
                    if node[0] != cur_node[0]:  # make sure we look at hte other end of the arc
                        if node[1] == arc[found] and node[2] == arc[found+1]:
                            arc[6] = node[0] # tell this arc that this node is its ending point                  
                            network, nodes = connect_network(network, nodes, node[0]) # and investigate downstream
                            break
    
    return network, nodes


def run_model(network, nodes, demand_per_person_kw_peak, mg_gen_cost_per_kw, mg_cost_per_m2, cost_wire_per_m, grid_cost_per_m2):
    """

    """

    # First calcaulte the off-grid cost for each unconnected settlement
    for node in nodes:
        if node[5] == 0:
            node[7] = node[4]*demand_per_person_kw_peak*mg_gen_cost_per_kw + node[3]*mg_cost_per_m2


    # Then we're ready to calculate the optimum grid extension.
    # This is done by expanding out from each already connected node,
    # finding the optimum connection of nearby nodes.
    # This is then compared to the off-grid cost and if better,
    # these nodes are marked as connected.
    # Then the loop continues until no new connections are found.

    # This function recurses through the network, dragging a current c_ values along with it.
    # These aren't returned, so are left untouched by aborted side-branch explorations.
    # The best b_ values are returned, and are updated whenever a better configuration is found.
    # Thus these will remmber the best solution including all side meanders.

    def find_best(nodes, network, index, prev_arc, b_pop, b_length, b_nodes, b_arcs, c_pop, c_length, c_nodes, c_arcs):
        if nodes[index][6] == 0:  # don't do anything with already connected nodes
            
            
            c_pop += nodes[index][4]
            c_length += network[prev_arc][8]
            c_nodes = c_nodes[:] + [index]
            c_arcs = c_arcs[:] + [prev_arc]
                  
            if c_pop/c_length > b_pop/b_length:
                b_pop = c_pop
                b_length = c_length
                b_nodes[:] = c_nodes[:]
                b_arcs[:] = c_arcs[:]
        
            connected_arcs = [network[arc_index] for arc_index in nodes[index][8]]
            for arc in connected_arcs:
                if arc[9] == 0 and arc[0] != prev_arc:

                    goto = 6 if arc[5] == index else 5  # make sure we look at the other end of the arc
                    nodes, network, b_pop, b_length, best_nodes, best_arcs = find_best(
                        nodes, network, arc[goto], arc[0], b_pop, b_length, b_nodes, b_arcs, c_pop, c_length, c_nodes, c_arcs)
                    
        return nodes, network, b_pop, b_length, b_nodes, b_arcs


    while True:  # keep looping until no further connections are added
        to_be_connected = []
        
        for node in nodes:
            if node[6] == 1:  # only start searches from currently connected nodes
                
                connected_arcs = [network[arc_index] for arc_index in node[8]]
                for arc in connected_arcs:
                    if arc[9] == 0:
                        goto = 6 if arc[5] == node[0] else 5
                        
                        # function call a bit of a mess with all the c_ and b_ values
                        nodes, network, b_length, b_pop, b_nodes, b_arcs = find_best(
                            nodes, network, arc[goto], arc[0], 0, 1e-9, [], [], 0, 1e-9, [], [])                

                        # calculate the mg and grid costs of the resultant configuration
                        best_nodes = [nodes[i] for i in b_nodes]
                        best_arcs = [network[i] for i in b_arcs]
                        mg_cost = sum([node[7] for node in best_nodes])
                        grid_cost = (cost_wire_per_m * sum(arc[8] for arc in best_arcs) + 
                                     grid_cost_per_m2 * sum([node[3] for node in best_nodes]))

                        if grid_cost < mg_cost:
                            # check if any nodes are already in to_be_connected
                            add = True
                            for index, item in enumerate(to_be_connected):
                                if set(b_nodes).intersection(item[1]):
                                    if b_pop/b_length < item[0]:
                                        del to_be_connected[index]
                                    else:
                                        add = False  # if the existing one is better, we don't add the new one
                                    break

                            if add:
                                to_be_connected.append((b_pop/b_length, b_nodes, b_arcs))
            
        # mark all to_be_connected as actually connected
        if len(to_be_connected) >= 1:
            print(len(to_be_connected))
            for item in to_be_connected:
                for node in item[1]:
                    nodes[node][6] = 1
                for arc in item[2]:
                    network[arc][9] = 1
        
        else:
            break  # exit the loop once nothing is added

    return network, nodes


def spatialise(network, nodes, clusters):
    """
    And then do a join to get the results back into a polygon shapefile
    """

    # prepare nodes and join with original clusters gdf
    nodes_df = pd.DataFrame(columns=['index', 'X', 'Y', 'area_m2', 'pop_sum', 'conn_start', 'conn_end',
                                      'og_cost', 'arcs'], data=nodes)
    nodes_df = nodes_df[['index', 'conn_end', 'og_cost']]
    clusters_joined = clusters.merge(nodes_df, how='left', left_index=True, right_index=True)

    # do the same for the network array
    network_df = pd.DataFrame(columns=['index', 'xs', 'ys', 'xe', 'ye', 'node_start', 'node_end',
                                       'existing', 'length', 'enabled'], data=network)
    network_geometry = [LineString([(arc[1], arc[2]), (arc[3], arc[4])]) for arc in network]
    network_gdf = gpd.GeoDataFrame(network_df, crs=clusters.crs, geometry=network_geometry)

    clusters_joined = clusters_joined.to_crs(epsg=4326)
    network_gdf = network_gdf.to_crs(epsg=4326)

    return network_gdf, clusters_joined
