'''
Created on Oct 8, 2015

@author: Patrick
'''
import time
import math

import bpy
import bmesh
import bgl

from mathutils import Vector, Matrix, Color, kdtree
from mathutils.bvhtree import BVHTree
from mathutils.geometry import intersect_point_line, intersect_line_plane
from bpy_extras import view3d_utils

from ..bmesh_fns import grow_selection_to_find_face, flood_selection_faces, edge_loops_from_bmedges_old, flood_selection_by_verts, flood_selection_edge_loop
from ..cut_algorithms import cross_section_2seeds_ver1, path_between_2_points
from .. import common_drawing
from ..common_utilities import bversion

class PolyLineKnife(object):
    '''
    A class which manages user placed points on an object to create a
    poly_line, adapted to the objects surface.
    '''

    ## Initializing 
    def __init__(self,context, cut_object, ui_type = 'DENSE_POLY'):   
        self.cut_ob = cut_object
        self.bme = bmesh.new()
        self.bme.from_mesh(cut_object.data)
        self.bme.verts.ensure_lookup_table()
        self.bme.edges.ensure_lookup_table()
        self.bme.faces.ensure_lookup_table()
        
        non_tris = [f for f in self.bme.faces if len(f.verts) > 3]
        #if len(non_tris):
            #geom = bmesh.ops.connect_verts_concave(self.bme, non_tris)
            #self.bme.verts.ensure_lookup_table()
            #self.bme.edges.ensure_lookup_table()
            #self.bme.faces.ensure_lookup_table()
        
        self.bvh = BVHTree.FromBMesh(self.bme)
        
        self.cyclic = False
        self.start_edge = None
        self.end_edge = None
        
        self.pts = []  #world space  self.cut_ob.matrix_world * local_points
        self.cut_pts = []  #local points
        self.normals = []
        
        self.face_map = []  #all the faces that user drawn poly line points fall upon
        self.face_changes = [] #the indices where the next point lies on a different face
        self.face_groups = dict()   #maps bmesh face index to all the points in user drawn polyline which fall upon it
        #self.prev_region = []
                                    #Important for multi cuts on a single 
        self.new_ed_face_map = dict()  #maps face index in bmesh to new edges created by bisecting
        
        self.ed_map = []  #existing edges in bmesh crossed by cut line.  list of type BMEdge
        self.new_cos = []  #location of crosses.  list of tyep Vector().  Does not include user clicked noew positions
        self.face_chain = set()  #all faces crossed by the cut curve. set of type BMFace
        
        
        self.non_man_eds = [ed.index for ed in self.bme.edges if not ed.is_manifold]
        self.non_man_ed_loops = edge_loops_from_bmedges_old(self.bme, self.non_man_eds)
        
        
   
        #print(self.non_man_ed_loops)
        self.non_man_points = []
        self.non_man_bmverts = []
        for loop in self.non_man_ed_loops:
            self.non_man_points += [self.cut_ob.matrix_world * self.bme.verts[ind].co for ind in loop]
            self.non_man_bmverts += [self.bme.verts[ind].index for ind in loop]
        if len(self.non_man_points):  
            kd = kdtree.KDTree(len(self.non_man_points))
            for i, v in enumerate(self.non_man_points):
                kd.insert(v, i)
                
            kd.balance()            
            self.kd = kd
        else:
            self.kd = None
            
        
        if ui_type not in {'SPARSE_POLY','DENSE_POLY', 'BEZIER'}:
            self.ui_type = 'SPARSE_POLY'
        else:
            self.ui_type = ui_type
                
        self.selected = -1
        self.hovered = [None, -1]
        
        self.grab_undo_loc = None
        self.start_edge_undo = None
        self.end_edge_undo = None
        
        self.mouse = (None, None)
        
        #keep up with these to show user
        self.bad_segments = []
        self.split = False
        self.perimeter_edges = []
        self.inner_faces = []
        self.face_seed = None
    
    ## Resets datastructures
    def reset_vars(self):
        '''
        TODOD, parallel workflow will make this obsolete
        '''
        self.cyclic = False  #for cuts entirely within the mesh
        self.start_edge = None #for cuts ending on non man edges
        self.end_edge = None  #for cuts ending on non man edges
        
        self.pts = []  #world points
        self.cut_pts = []  #local points
        self.normals = []
        self.face_map = []
        self.face_changes = []
        self.new_cos = []
        self.ed_map = []
        
        self.face_chain = set()  #all faces crossed by the cut curve
                
        self.selected = -1
        self.hovered = [None, -1]
        
        self.grab_undo_loc = None
        self.mouse = (None, None)
        
        #keep up with these to show user
        self.bad_segments = []
        self.face_seed = None
        

    ## ****** POLYLINE/POINT MANIPULATION *****
    
    ## Add's a point to the trim line. 
    def click_add_point(self,context,x,y):
        '''
        x,y = event.mouse_region_x, event.mouse_region_y
        
        this will add a point into the bezier curve or
        close the curve into a cyclic curve
        '''

        # The next couple chunks of code are repetive. They exist in other functions.
        region = context.region
        rv3d = context.region_data
        coord = x, y
        view_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        ray_target = ray_origin + (view_vector * 1000)
        mx = self.cut_ob.matrix_world
        imx = mx.inverted()
    
        if bversion() < '002.077.000':
            loc, no, face_ind = self.cut_ob.ray_cast(imx * ray_origin, imx * ray_target)
            if face_ind == -1: 
                self.selected = -1
                return
        else:
            res, loc, no, face_ind = self.cut_ob.ray_cast(imx * ray_origin, imx * ray_target - imx * ray_origin)
        
            if not res:
                self.selected = -1
                return


        # if user started on edge and is currently hovering over non man edge
        if self.hovered[0] and 'NON_MAN' in self.hovered[0]:
            
            # unselect if it's cyclic and non manifold 
            if self.cyclic:
                self.selected = -1
                return
            
            ed, wrld_loc = self.hovered[1] # hovered[1] is tuple
            
            if len(self.pts) == 0:
                self.start_edge = ed

            elif len(self.pts) and not self.start_edge:
                self.selected = -1
                return
            
            elif len(self.pts) and self.start_edge:
                self.end_edge = ed
                
            self.pts += [wrld_loc] 
            self.cut_pts += [imx * wrld_loc]
            #self.cut_pts += [loc]
            self.face_map += [ed.link_faces[0].index]
            self.normals += [view_vector]
            self.selected = len(self.pts) -1

        # Add pont information to datastructures if nothing is being hovered over
        if self.hovered[0] == None and not self.end_edge:  #adding in a new point at end
            self.pts += [mx * loc]  #Store the world location of the click
            self.cut_pts += [loc]   #store the local location of the click (in cut_object space)
            #self.normals += [no]
            self.normals += [view_vector] #try this, because fase normals are difficult
            self.face_map += [face_ind]
            self.selected = len(self.pts) -1
                
        # If you click point, set it's index to 'selected'
        if self.hovered[0] == 'POINT':
            self.selected = self.hovered[1]
            # If click is first point
            if self.hovered[1] == 0 and not self.start_edge:  
                #can not  toggle cyclic any more, once it's on it remains on
                if self.cyclic:
                    return
                else:
                    self.cyclic = True
            return
         
        # If an edge is clicked, cut in a new point
        elif self.hovered[0] == 'EDGE':  
            self.pts.insert(self.hovered[1]+1, mx * loc)
            self.cut_pts.insert(self.hovered[1]+1, loc)
            self.normals.insert(self.hovered[1]+1, view_vector)
            self.face_map.insert(self.hovered[1]+1, face_ind)
            self.selected = self.hovered[1] + 1
            
            if len(self.new_cos):
                self.make_cut()
            return
    
    ## Delete's a point from the trim line. 
    def click_delete_point(self, mode = 'mouse'):
        if mode == 'mouse':
            if self.hovered[0] != 'POINT': return
            
            self.pts.pop(self.hovered[1])
            self.cut_pts.pop(self.hovered[1])
            self.normals.pop(self.hovered[1])
            self.face_map.pop(self.hovered[1])

            # some kinds of deletes make cyclic false again
            if len(self.pts) <= 2 or self.hovered[1] == 0: self.cyclic = False

            # selected point should only change if selected is deleted
            if self.selected > self.hovered[1]: self.selected -= 1
            
            if self.end_edge != None and self.hovered[1] == len(self.cut_pts): #notice not -1 because we popped 
                print('deteted last non man edge')
                self.end_edge = None
                self.new_cos = []
                self.selected = -1
                
                return
              
        
        else:
            if self.selected == -1: return
            self.pts.pop(self.selected)
            self.cut_pts.pop(self.selected)
            self.normals.pop(self.selected)
            self.face_map.pop(self.selected)
            
        if len(self.new_cos):
            self.make_cut()

    ## Initiates a grab if point is selected
    def grab_initiate(self):
        if self.selected != -1:
            self.grab_undo_loc = self.pts[self.selected]
            self.start_edge_undo = self.start_edge
            self.end_edge_undo = self.end_edge
            return True
        else:
            return False
       
    def grab_mouse_move(self,context,x,y):
        
       
        region = context.region
        rv3d = context.region_data
        coord = x, y
        view_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        ray_target = ray_origin + (view_vector * 1000)
        mx = self.cut_ob.matrix_world
        imx = mx.inverted()

        if bversion() < '002.077.000':
            loc, no, face_ind = self.cut_ob.ray_cast(imx * ray_origin, imx * ray_target)
            if face_ind == -1: 
                self.grab_cancel()
                return
        else:
            res, loc, no, face_ind = self.cut_ob.ray_cast(imx * ray_origin, imx * ray_target - imx * ray_origin)
        
            if not res:
                self.grab_cancel()
                return
            
        #check if first or end point and it's a non man edge!   
        if self.selected == 0 and self.start_edge or self.selected == (len(self.pts) -1) and self.end_edge:
        
            co3d, index, dist = self.kd.find(mx * loc)

            #get the actual non man vert from original list
            close_bmvert = self.bme.verts[self.non_man_bmverts[index]] #stupid mapping, unreadable, terrible, fix this, because can't keep a list of actual bmverts
            close_eds = [ed for ed in close_bmvert.link_edges if not ed.is_manifold]
            loc3d_reg2D = view3d_utils.location_3d_to_region_2d
            
            if len(close_eds) != 2:
                self.grab_cancel()
                return
                
            bm0 = close_eds[0].other_vert(close_bmvert)
            bm1 = close_eds[1].other_vert(close_bmvert)
        
            a0 = bm0.co
            b   = close_bmvert.co
            a1  = bm1.co 
            
            inter_0, d0 = intersect_point_line(loc, a0, b)
            inter_1, d1 = intersect_point_line(loc, a1, b)
            
            screen_0 = loc3d_reg2D(region, rv3d, mx * inter_0)
            screen_1 = loc3d_reg2D(region, rv3d, mx * inter_1)
            screen_v = loc3d_reg2D(region, rv3d, mx * b)
            
            screen_d0 = (self.mouse - screen_0).length
            screen_d1 = (self.mouse - screen_1).length
            screen_dv = (self.mouse - screen_v).length
            
            if 0 < d0 <= 1 and screen_d0 < 60:
                ed, pt = close_eds[0], inter_0
                
            elif 0 < d1 <= 1 and screen_d1 < 60:
                ed, pt = close_eds[1], inter_1
                
            elif screen_dv < 60:
                if abs(d0) < abs(d1):
                    ed, pt = close_eds[0], b
                    
                else:
                    ed, pt = close_eds[1], b
                    
            else:
                self.grab_cancel()
                return
            
            if self.selected == 0:
                self.start_edge = ed
            else:
                self.end_edge = ed
            
            self.pts[self.selected] = mx * pt
            self.cut_pts[self.selected] = pt
            self.normals[self.selected] = view_vector
            self.face_map[self.selected] = ed.link_faces[0].index             
        else:
            self.pts[self.selected] = mx * loc
            self.cut_pts[self.selected] = loc
            self.normals[self.selected] = view_vector
            self.face_map[self.selected] = face_ind
        
    def grab_cancel(self):
        self.pts[self.selected] = self.grab_undo_loc
        self.start_edge = self.start_edge_undo
        self.end_edge = self.end_edge_undo
        return
    
    def grab_confirm(self):
        self.grab_undo_loc = None
        self.start_edge_undo = None
        self.end_edge_undo = None
        return
 
    ## Makes the sketch and rebuilds the list of input points depending on the sketch
    def make_sketch(self, hovered_start, sketch_points, view_vector):
        hover_start = hovered_start[1]
        hovered_end = self.hovered
        hover_end = hovered_end[1]
        view_vectors = [view_vector]*len(sketch_points)

        ## Non-manifold sketches first
        
        # ending on non manifold edge
        if hovered_end[0] and "NON_MAN" in hovered_end[0]:
            self.pts += sketch_points + [hovered_end[1][1]]
            self.normals += view_vectors + [view_vector]
            self.end_edge = hovered_end[1][0]

        # starting on non manifold edge
        elif hovered_start[0] and "NON_MAN" in hovered_start[0]: 
            self.pts += sketch_points
            self.normals += view_vectors
            self.start_edge = hovered_start[1][0]

        ## then manifold sketches

        #User is not connecting back to polyline
        elif hovered_end[0] == None:  

            # Do nothing if it's cyclic
            if self.cyclic:
                pass

            # add the points in at beginning of line
            elif hover_start == 0: 
                self.pts = sketch_points[::-1] + self.pts[:]
                self.normals = view_vectors + self.normals 
                self.pts = self.pts[::-1]
                self.normals = self.normals[::-1]
            
            # add points at end of line
            elif hover_start == len(self.pts) - 1:
                self.cyclic = False  # Correction for having set cyclic equal to True previously
                self.pts += sketch_points
                self.normals += view_vectors

            # add points midway into the line and trim the rest
            else:  #if the last hovered was not the endpoint of the polyline, need to trim and append
                self.pts = self.pts[:hover_start] + sketch_points
                self.normals = self.normals[:hover_start] + view_vectors
        
        # user is replacing a segment with a sketch because they initiaiated and terminated the sketch on the line.
        else:  
            # if start and stop sketch point is same, don't do anything, unless their is only 1 point.
            if hover_end == hover_start:
                if len(self.pts) == 1: 
                    self.pts += sketch_points
                    self.normals += view_vectors
                    self.cyclic = True

            elif self.cyclic:
                # figure out ammount of points between hover_end and hover_start on both sides
                last_point_index = len(self.pts) - 1
                num_between = abs(hover_end - hover_start) - 1
                if hover_start < hover_end:  num_between_thru_origin = (last_point_index - hover_end) + hover_start
                else: num_between_thru_origin = (last_point_index - hover_start) + hover_end

                # path through origin point is shorter so cut them out points on those segments/points
                if num_between_thru_origin <= num_between:
                    if hover_start > hover_end:  
                        self.pts = self.pts[hover_end:hover_start] + sketch_points
                        self.normals = self.normals[hover_end:hover_start] + view_vectors
                    else:
                        self.pts = sketch_points + self.pts[hover_end:hover_start:-1]
                        self.normals = view_vectors + self.normals[hover_end:hover_start:-1]

                # path not passing through origin point is shorter so cut points on this path
                else:
                    if hover_start > hover_end:  
                        self.pts = self.pts[:hover_end] + sketch_points[::-1] + self.pts[hover_start:] 
                        self.normals = self.normals[:hover_end] + view_vectors + self.normals[hover_start:] 
                    else:
                        
                        self.pts = self.pts[:hover_start] + sketch_points + self.pts[hover_end:] 
                        self.normals = self.normals[:hover_start] + view_vectors + self.normals[hover_end:]

            else:
                #drawing "upstream" relative to self.pts indexing (towards index 0)
                if hover_start > hover_end:  
                    # connecting the ends (making cyclic)
                    if hover_end == 0 and hover_start == len(self.pts) - 1:
                        self.pts += sketch_points
                        self.normals += view_vectors
                        self.cyclic = True
                    # add sketch points in
                    else:
                        self.pts = self.pts[:hover_end] + sketch_points[::-1] + self.pts[hover_start:]
                        self.normals = self.normals[:hover_end] + view_vectors + self.normals[hover_start:]
                
                #drawing "downstream" relative to self.pts indexing (away from index 0)
                else:
                    # making cyclic
                    if hover_end == len(self.pts) - 1 and hover_start == 0:
                        self.pts = sketch_points + self.pts[::-1]
                        self.normals = view_vectors + self.normals[::-1]
                        self.cyclic = True 
                    # when no points are out
                    elif hover_end == 0: 
                        self.pts = self.pts[:1] + sketch_points
                        self.normals = self.normals[:1] + view_vectors
                        self.cyclic = True
                    # adding sketch points in
                    else:   
                        self.pts = self.pts[:hover_start] + sketch_points + self.pts[hover_end:]
                        self.normals = self.normals[:hover_start]  + view_vectors + self.normals[hover_end:]
        

    ## ****** HOVER *****

    ## Called everytime mouse is moved while in wait mode.
    def hover(self,context,x,y):
        '''
        hovering happens in mixed 3d and screen space, 20 pixels thresh for points, 30 for edges
        40 for non_man
        '''

        region = context.region
        rv3d = context.region_data
        coord = x, y
        self.mouse = Vector((x, y))
        
        loc3d_reg2D = view3d_utils.location_3d_to_region_2d
        
        view_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        ray_target = ray_origin + (view_vector * 1000)
        mx = self.cut_ob.matrix_world
        imx = mx.inverted()

        #loc, no, face_ind = self.cut_ob.ray_cast(imx * ray_origin, imx * ray_target)
        ''' '''
        ## TODO:Right now the next if and else statements are obsolete
        if bversion() < '002.077.000':
            loc, no, face_ind = self.cut_ob.ray_cast(imx * ray_origin, imx * ray_target)
            if face_ind == -1: 
                #do some shit
                pass
        else:
            res, loc, no, face_ind = self.cut_ob.ray_cast(imx * ray_origin, imx * ray_target - imx * ray_origin)
            if not res:
                #do some shit
                pass
            
        # if no input points...
        if len(self.pts) == 0:
            self.hovered = [None, -1]
            self.hover_non_man(context, x, y)
            return

        ## Both of the next functions are used to find distances
        def dist(v):
            if v == None:
                print('v off screen')
                return 100000000
            diff = v - Vector((x,y))
            return diff.length
        
        
        def dist3d(v3):
            if v3 == None:
                return 100000000
            delt = v3 - self.cut_ob.matrix_world * loc
            return delt.length
        
        closest_3d_point = min(self.pts, key = dist3d)
        point_screen_dist = dist(loc3d_reg2D(context.region, context.space_data.region_3d, closest_3d_point))
        
        # If an input point is less than 20(some unit) away, stop and set hovered to the input point
        if point_screen_dist  < 20:
            self.hovered = ['POINT',self.pts.index(closest_3d_point)]
            return

        # If there is 1 input point, stop and set hovered to None
        if len(self.pts) < 2: 
            self.hovered = [None, -1]
            return
        
        ## ?? What is happening here 
        line_inters3d = []
        for i in range(len(self.pts)):   
            
            nexti = (i + 1) % len(self.pts)
            if next == 0 and not self.cyclic:
                self.hovered = [None, -1]
                return

             
            intersect3d = intersect_point_line(self.cut_ob.matrix_world * loc, self.pts[i], self.pts[nexti])
            
            if intersect3d != None:
                dist3d = (intersect3d[0] - loc).length
                bound3d = intersect3d[1]
                if  (bound3d < 1) and (bound3d > 0):
                    line_inters3d += [dist3d]
                    #print('intersect line3d success')
                else:
                    line_inters3d += [1000000]
            else:
                line_inters3d += [1000000]
        
        ## ?? And here
        i = line_inters3d.index(min(line_inters3d))
        nexti = (i + 1) % len(self.pts)  
        
        ## ?? And here
        a  = loc3d_reg2D(context.region, context.space_data.region_3d,self.pts[i])
        b = loc3d_reg2D(context.region, context.space_data.region_3d,self.pts[nexti])
        
        ## ?? and here, obviously, its stopping and setting hovered to EDGE, but how?
        if a and b:
            intersect = intersect_point_line(Vector((x,y)).to_3d(), a.to_3d(),b.to_3d())      
            dist = (intersect[0].to_2d() - Vector((x,y))).length_squared
            bound = intersect[1]
            if (dist < 400) and (bound < 1) and (bound > 0):
                self.hovered = ['EDGE', i]        
                return
             
        ## Multiple points, but not hovering over edge or point.
        self.hovered = [None, -1]
        
        if self.start_edge != None:
            self.hover_non_man(context, x, y)  #todo, optimize because double ray cast per mouse move!
          
    
        '''
        only needed if processing an outside mesh
        '''
        locs = []
        self.face_map = []
        #self.normals = [] for now, leave normals from view direction
        self.face_changes = []
        self.face_groups = dict()
        
        mx = self.cut_ob.matrix_world
        imx = mx.inverted()
        
        last_face_ind = None
        for i, v in enumerate(self.pts):
            if bversion() < '002.077.000':
                loc, no, ind, d = self.bvh.find(imx * v)
            else:
                loc, no, ind, d = self.bvh.find_nearest(imx * v)
                
            self.face_map.append(ind)
            locs.append(loc)
            
            if i == 0:
                last_face_ind = ind
                group = [i]
                print('first face group index')
                print((ind,group))
                
            if ind != last_face_ind: #we have found a new face
                self.face_changes.append(i-1)
                
                if last_face_ind not in self.face_groups: #previous face has not been mapped before
                    self.face_groups[last_face_ind] = group
                    last_face_ind = ind
                    group = [i]
                else:
                    print('group already in dictionary')
                    exising_group = self.face_groups[last_face_ind]
                    if 0 not in exising_group:
                        print('LOOKS LIKE WE CLICKED SAME FACE MULTIPLE TIMES')
                        print('YOUR PROGRAMMER IS NOT SMART ENOUGH FOR THIS')
                        #TODO....GENERATE SOME ERROR
                        #TODO....REMOVE SELF INTERSECTIONS IN ORIGINAL PATH
                        
                    self.face_groups[last_face_ind] = group + exising_group #we have wrapped, add this group to the old
            
            else:
                if i != 0:
                    group += [i]
            #double check for the last point
            if i == len(self.pts) - 1:  #
                if ind != self.face_map[0]:  #we didn't click on the same face we started on
                    
                    if self.cyclic:
                        self.face_changes.append(i)
                        
                    if ind not in self.face_groups:
                        print('final group not added to dictionary yet')
                        print((ind, group))
                        self.face_groups[ind] = group
                    
                    else:
                        print('group already in dictionary')
                        exising_group = self.face_groups[ind]
                        if 0 not in exising_group:
                            print('LOOKS LIKE WE CROSSED SAME FACE MULTIPLE TIMES')
                            print('YOUR PROGRAMMER IS NOT SMART ENOUGH FOR THIS')
                        self.face_groups[ind] = group + exising_group
                        
                else:
                    print('group already in dictionary')
                    exising_group = self.face_groups[ind]
                    if 0 not in exising_group:
                        print('LOOKS LIKE WE CROSSED SAME FACE MULTIPLE TIMES')
                        print('YOUR PROGRAMMER IS NOT SMART ENOUGH FOR THIS')
                    self.face_groups[ind] = group + exising_group
                              
        self.cut_pts = locs
        
        #clean up face groups if necessary
        #TODO, get smarter about not adding in these
        if not self.cyclic and not self.split:  # I added in 'and not self.split', but I'm not sure if this is smart...
            if self.start_edge:
                s_ind = self.start_edge.link_faces[0].index ##TODO 
                if s_ind in self.face_groups:
                    v_group = self.face_groups[s_ind]
                    if len(v_group) == 1:
                        print('remove first face from face groups')
                        del self.face_groups[s_ind]
                    elif len(v_group) > 1:
                        print('remove first vert from first face group')
                        v_group.pop(0)
                        self.face_groups[s_ind] = v_group
            if self.end_edge:
                e_ind = self.end_edge.link_faces[0].index        
                if e_ind in self.face_groups:
                    v_group = self.face_groups[e_ind]
                    if len(v_group) == 1:
                        print('remove last face from face groups')
                        del self.face_groups[e_ind]
                    elif len(v_group) > 1:
                        print('remove last vert from last face group')
                        v_group.pop()
                        self.face_groups[e_ind] = v_group

    def hover_non_man(self,context,x,y):
        region = context.region
        rv3d = context.region_data
        coord = x, y
        self.mouse = Vector((x, y))
        
        loc3d_reg2D = view3d_utils.location_3d_to_region_2d
        
        view_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        ray_target = ray_origin + (view_vector * 1000)
        mx = self.cut_ob.matrix_world
        imx = mx.inverted()
        if bversion() < '002.077.000':
            loc, no, face_ind = self.cut_ob.ray_cast(imx * ray_origin, imx * ray_target)
            
        else:
            res, loc, no, face_ind = self.cut_ob.ray_cast(imx * ray_origin, imx * ray_target - imx * ray_origin)
        
        if len(self.non_man_points):
            co3d, index, dist = self.kd.find(mx * loc)

            #get the actual non man vert from original list
            close_bmvert = self.bme.verts[self.non_man_bmverts[index]] #stupid mapping, unreadable, terrible, fix this, because can't keep a list of actual bmverts
            close_eds = [ed for ed in close_bmvert.link_edges if not ed.is_manifold]
            if len(close_eds) == 2:
                bm0 = close_eds[0].other_vert(close_bmvert)
                bm1 = close_eds[1].other_vert(close_bmvert)
            
                a0 = bm0.co
                b   = close_bmvert.co
                a1  = bm1.co 
                
                inter_0, d0 = intersect_point_line(loc, a0, b)
                inter_1, d1 = intersect_point_line(loc, a1, b)
                
                screen_0 = loc3d_reg2D(region, rv3d, mx * inter_0)
                screen_1 = loc3d_reg2D(region, rv3d, mx * inter_1)
                screen_v = loc3d_reg2D(region, rv3d, mx * b)
                
                if not screen_0 and screen_1 and screen_v:
                    return
                screen_d0 = (self.mouse - screen_0).length
                screen_d1 = (self.mouse - screen_1).length
                screen_dv = (self.mouse - screen_v).length
                
                if 0 < d0 <= 1 and screen_d0 < 20:
                    self.hovered = ['NON_MAN_ED', (close_eds[0], mx*inter_0)]
                    return
                elif 0 < d1 <= 1 and screen_d1 < 20:
                    self.hovered = ['NON_MAN_ED', (close_eds[1], mx*inter_1)]
                    return
                elif screen_dv < 20:
                    if abs(d0) < abs(d1):
                        self.hovered = ['NON_MAN_VERT', (close_eds[0], mx*b)]
                        return
                    else:
                        self.hovered = ['NON_MAN_VERT', (close_eds[1], mx*b)]
                        return
 

    ## ***** CUTTING & PREP *****

    ## Fills data strucutures based on trim line and groups input points with polygons in the cut object
    def preprocess_points(self):
        '''
        Accomodate for high density cutting on low density geometry
        '''
        if not self.cyclic and not (self.start_edge != None and self.end_edge != None):
            print('not ready!')
            return
        #self.normals = [] for now, leave normals from view direction
        self.face_changes = []
        self.face_groups = dict()
        last_face_ind = None

        # Loop through each input point
        for i, v in enumerate(self.pts):

            # if loop is on first input point
            if i == 0:
                last_face_ind = self.face_map[i]
                group = [i]
                print('first face group index')
                print((self.face_map[i],group))
                
            # if we have found a new face
            if self.face_map[i] != last_face_ind: 
                self.face_changes.append(i-1) #this index in cut points, represents an input point that is on a face which has not been evaluted previously
                #Face changes might better be described as edge crossings
                
                if last_face_ind not in self.face_groups: #previous face has not been mapped before
                    self.face_groups[last_face_ind] = group
                    last_face_ind = self.face_map[i]
                    group = [i]
                else:
                    print('group already in dictionary')
                    exising_group = self.face_groups[last_face_ind]
                    if 0 not in exising_group:
                        print('LOOKS LIKE WE CLICKED ON SAME FACE MULTIPLE TIMES')
                        print('YOUR PROGRAMMER IS NOT SMART ENOUGH FOR THIS')
                        print('THEREFORE SOME VERTS MAY NOT BE ACCOUNTED FOR...')
                    
                    
                    else:
                        self.face_groups[last_face_ind] = group + exising_group #we have wrapped, add this group to the old
            
            else:
                if i != 0:
                    group += [i]
            #double check for the last point
            if i == len(self.pts) - 1:  #
                if self.face_map[i] != self.face_map[0]:  #we didn't click on the same face we started on
                    
                    if self.cyclic:
                        self.face_changes.append(i)
                        
                    if self.face_map[i] not in self.face_groups:
                        #print('final group not added to dictionary yet')
                        #print((self.face_map[i], group))
                        self.face_groups[self.face_map[i]] = group
                    
                    else:
                        #print('group already in dictionary')
                        exising_group = self.face_groups[self.face_map[i]]
                        if 0 not in exising_group:
                            print('LOOKS LIKE WE CROSSED SAME FACE MULTIPLE TIMES')
                            print('YOUR PROGRAMMER IS NOT SMART ENOUGH FOR THIS')
                        else:
                            self.face_groups[self.face_map[i]] = group + exising_group
                        
                else:
                    #print('group already in dictionary')
                    exising_group = self.face_groups[self.face_map[i]]
                    if 0 not in exising_group:
                        print('LOOKS LIKE WE CROSSED SAME FACE MULTIPLE TIMES')
                        print('YOUR PROGRAMMER IS NOT SMART ENOUGH FOR THIS')
                    else:
                        self.face_groups[self.face_map[i]] = group + exising_group
        
        #clean up face groups if necessary
        #TODO, get smarter about not adding in these
        if not self.cyclic:
            s_ind = self.start_edge.link_faces[0].index
            e_ind = self.end_edge.link_faces[0].index
            
            if s_ind in self.face_groups:
                v_group = self.face_groups[s_ind]
                if len(v_group) == 1:
                    print('remove first face from face groups')
                    del self.face_groups[s_ind]
                elif len(v_group) > 1:
                    print('remove first vert from first face group')
                    v_group.pop(0)
                    self.face_groups[s_ind] = v_group
                    
            if e_ind in self.face_groups:
                v_group = self.face_groups[e_ind]
                if len(v_group) == 1:
                    print('remove last face from face groups')
                    del self.face_groups[e_ind]
                elif len(v_group) > 1:
                    print('remove last vert from last face group')
                    v_group.pop()
                    self.face_groups[e_ind] = v_group
        
        print('FACE GROUPS')
        print(self.face_groups)
               
    def click_seed_select(self, context, x, y):
        
        region = context.region
        rv3d = context.region_data
        coord = x, y
        view_vector = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        ray_target = ray_origin + (view_vector * 1000)
        mx = self.cut_ob.matrix_world
        imx = mx.inverted()

        if bversion() < '002.077.000':
            loc, no, face_ind = self.cut_ob.ray_cast(imx * ray_origin, imx * ray_target)
            
        else:
            res, loc, no, face_ind = self.cut_ob.ray_cast(imx * ray_origin, imx * ray_target - imx * ray_origin)
        
            
        if face_ind != -1 and face_ind not in [f.index for f in self.face_chain]:
            self.face_seed = self.bme.faces[face_ind]
            print('face selected!!')
            return 1
            
        elif face_ind != -1 and face_ind  in [f.index for f in self.face_chain]:
            print('face too close to boundary')
            return -1
        else:
            self.face_seed = None
            print('face not selected')
            return 0
    
    def preview_region(self):
        if self.face_seed == None:
            return
        
        #face_set = flood_selection_faces(self.bme, self.face_chain, self.face_seed, max_iters = 5000)
        #self.prev_region = [f.calc_center_median() for f in face_set]

    # Makes the cut depending on the kind desired     
    def make_cut(self):
        if self.split: return #already did this, no going back!
        mx = self.cut_ob.matrix_world
        imx = mx.inverted()
        print('\n')
        print('BEGIN CUT ON POLYLINE')
        
        self.new_cos = []  #New coordinates created by intersections of mesh edges with cut segments
        self.ed_map = []  #smarter thing to do might be edge_map = {}  edge_map[ed] = co. Can't cross an edge twice because dictionary reqires
        
        
        self.face_chain = set()
        self.preprocess_points()  #put things into different data strucutures and grouping input points with polygons in the cut object
        self.bad_segments = []
        
        self.new_ed_face_map = dict()
        
        #print('there are %i cut points' % len(self.cut_pts))
        #print('there are %i face changes' % len(self.face_changes))

        # iterate through each input point that changes a face
        for m, ind in enumerate(self.face_changes):

   
            
            ## first time through and non-manifold edge cut 
            if m == 0 and not self.cyclic:
                self.ed_map += [self.start_edge]
                #self.new_cos += [imx * self.cut_pts[0]]
                self.new_cos += [self.cut_pts[0]]
                
                
                #self.new_ed_face_map[0] = self.start_edge.link_faces[0].index
                
                #print('not cyclic...come back to me')
                #continue
            
            #n_p1 = (m + 1) % len(self.face_changes)
            #ind_p1 = self.face_changes[n_p1]

            n_p1 = (ind + 1) % len(self.cut_pts)  #The index of the next cut_pt (input point)
            ind_p1 = self.face_map[n_p1]  #the face in the cut object which the next cut point falls upon
            
            
            n_m1 = (ind - 1)
            ind_m1 = self.face_map[n_m1]
            #print('walk on edge pair %i, %i' % (m, n_p1))
            #print('original faces in mesh %i, %i' % (self.face_map[ind], self.face_map[ind_p1]))
            
            if n_p1 == 0 and not self.cyclic:
                print('not cyclic, we are done here')
                break
            
            f0 = self.bme.faces[self.face_map[ind]]  #<<--- Actualy get the BMFace from the cut_object BMEesh
            self.face_chain.add(f0)
            
            f1 = self.bme.faces[self.face_map[n_p1]] #<<--- Actualy get the BMFace from the cut_object BMEesh
            
            ###########################
            ## Define the cutting plane for this segment#
            ############################
            
            no0 = self.normals[ind]  #direction the user was looking when defining the cut
            no1 = self.normals[n_p1]  #direction the user was looking when defining the cut
            surf_no = imx.to_3x3() * no0.lerp(no1, 0.5)  #must be a better way.

            e_vec = self.cut_pts[n_p1] - self.cut_pts[ind]
            
            
            #define
            cut_no = e_vec.cross(surf_no)
                
            #cut_pt = .5*self.cut_pts[ind_p1] + 0.5*self.cut_pts[ind]
            cut_pt = .5*self.cut_pts[n_p1] + 0.5*self.cut_pts[ind]
    
            #find the shared edge,, check for adjacent faces for this cut segment
            cross_ed = None
            for ed in f0.edges:
                if f1 in ed.link_faces:
                    cross_ed = ed
                    self.face_chain.add(f1)
                    break
                
            #if no shared edge, need to cut across to the next face    
            if not cross_ed:
                if self.face_changes.index(ind) != 0:
                    p_face = self.bme.faces[self.face_map[ind-1]]  #previous face to try and be smart about the direction we are going to walk
                else:
                    p_face = None
                
                vs = []
                epp = .0000000001
                use_limit = True
                attempts = 0
                while epp < .0001 and not len(vs) and attempts <= 5:
                    attempts += 1
                    vs, eds, eds_crossed, faces_crossed, error = path_between_2_points(
                        self.bme, 
                        self.bvh,       
                        #self.cut_pts[ind], self.cut_pts[ind_p1],
                        self.cut_pts[ind], self.cut_pts[n_p1], 
                        max_tests = 10000, debug = True, 
                        prev_face = p_face,
                        use_limit = use_limit)
                    if len(vs) and error == 'LIMIT_SET':
                        vs = []
                        use_limit = False
                        print('Limit was too limiting, relaxing that consideration')
                        
                    elif len(vs) == 0 and error == 'EPSILON':
                        print('Epsilon was too small, relaxing epsilon')
                        epp *= 10
                    elif len(vs) == 0 and error:
                        print('too bad, couldnt adjust due to ' + error)
                        print(p_face)
                        print(f0)
                        break
                
                if not len(vs):
                    print('\n')
                    print('CUTTING METHOD')
                    
                    vs = []
                    epp = .00000001
                    use_limit = True
                    attempts = 0
                    while epp < .0001 and not len(vs) and attempts <= 10:
                        attempts += 1
                        vs, eds, eds_crossed, faces_crossed, error = cross_section_2seeds_ver1(
                            self.bme,
                            cut_pt, cut_no, 
                            f0.index,self.cut_pts[ind],
                            #f1.index, self.cut_pts[ind_p1],
                            f1.index, self.cut_pts[n_p1],
                            max_tests = 10000, debug = True, prev_face = p_face,
                            epsilon = epp)
                        if len(vs) and error == 'LIMIT_SET':
                            vs = []
                            use_limit = False
                        elif len(vs) == 0 and error == 'EPSILON':
                            epp *= 10
                        elif len(vs) == 0 and error:
                            print('too bad, couldnt adjust due to ' + error)
                            print(p_face)
                            print(f0)
                            break
                        
                if len(vs):
                    #do this before we add in any points
                    if len(self.new_cos) > 1:
                        self.new_ed_face_map[len(self.new_cos)-1] = self.face_map[ind]
                        
                    elif len(self.new_cos) == 1 and m ==1 and not self.cyclic:
                        self.new_ed_face_map[len(self.new_cos)-1] = self.face_map[ind]
                    for v,ed in zip(vs,eds_crossed):
                        self.new_cos.append(v)
                        self.ed_map.append(ed)
                    
                    print('crossed %i faces' % len(faces_crossed))   
                    self.face_chain.update(faces_crossed)
                        
                    if ind == len(self.face_changes) - 1 and self.cyclic:
                        print('This is the loop closing segment.  %i' % len(vs))
                
                
                else:  #we failed to find the next face in the face group
                    self.bad_segments.append(ind)
                    print('cut failure!!!')
                
                if ((not self.cyclic) and
                    m == (len(self.face_changes) - 1) and
                    self.end_edge.link_faces[0].index == f1.index
                    ):
                
                    print('end to the non manifold edge while walking multiple faces')
                    self.ed_map += [self.end_edge]
                    self.new_cos += [self.cut_pts[-1]]
                    self.new_ed_face_map[len(self.new_cos)-2] = f1.index
                
                continue
            
            p0 = cross_ed.verts[0].co
            p1 = cross_ed.verts[1].co
            v = intersect_line_plane(p0,p1,cut_pt,cut_no)
            if v:
                self.new_cos.append(v)
                self.ed_map.append(cross_ed)
                if len(self.new_cos) > 1:
                    self.new_ed_face_map[len(self.new_cos)-2] = self.face_map[ind]
            
            if ((not self.cyclic) and
                m == (len(self.face_changes) - 1) and
                self.end_edge.link_faces[0].index == f1.index
                ):
                
                print('end to the non manifold edge jumping single face')
                self.ed_map += [self.end_edge]
                self.new_cos += [self.cut_pts[-1]]
                self.new_ed_face_map[len(self.new_cos)-2] = f1.index
                          
    def smart_make_cut(self):
        if len(self.new_cos) == 0:
            print('havent made initial cut yet')
            self.make_cut()
    
        old_fcs = self.face_changes
        old_fgs = self.face_groups
        
        self.preprocess_points()


    ## ****** OTHER *****
                    
    def confirm_cut_to_mesh(self):
        
        if len(self.bad_segments): return  #can't do this with bad segments!!
        
        if self.split: return #already split! no going back

        self.calc_ed_pcts()

        if len(self.ed_map) != len(set(self.ed_map)):  #doubles in ed dictionary
            
            print('doubles in the edges crossed!!')
            print('ideally, this will turn  the face into an ngon for simplicity sake')
            seen = set()
            new_eds = []
            new_cos = []
            removals = []

            for i, ed in enumerate(self.ed_map):
                if ed not in seen and not seen.add(ed):
                    new_eds += [ed]
                    new_cos += [self.new_cos[i]]
                else:
                    removals.append(ed.index)
            
            print('these are the edge indices which were removed to be only cut once ')
            print(removals)
            
            self.ed_map = new_eds
            self.new_cos = new_cos
            
        for v in self.bme.verts:
            v.select_set(False)
        for ed in self.bme.edges:
            ed.select_set(False)  
        for f in self.bme.faces:
            f.select_set(False)
                
        start = time.time()
        print('bisecting edges')
        geom =  bmesh.ops.bisect_edges(self.bme, edges = self.ed_map,cuts = 1,edge_percents = {})
        new_bmverts = [ele for ele in geom['geom_split'] if isinstance(ele, bmesh.types.BMVert)]
        
        #assigned new verts their locations
        for v, co in zip(new_bmverts, self.new_cos):
            v.co = co
            #v.select_set(True)
        
        finish = time.time()
        print('Took %f seconds to bisect edges' % (finish-start))
        start = finish
        
        ##########################################################
        ########## Connect all the newly crated verts ############
        ed_geom = bmesh.ops.connect_verts(self.bme, verts = new_bmverts, faces_exclude = [], check_degenerate = False)
        new_edges = ed_geom['edges']
        if self.cyclic:
            new_edges.reverse()
            new_edges = new_edges[1:] + [new_edges[0]]
        
            
        finish = time.time()
        print('took %f seconds to connect the verts and %i new edges were created' % ((finish-start), len(new_edges)))
        start = finish
        
        self.bme.verts.ensure_lookup_table()
        self.bme.edges.ensure_lookup_table()
           
        ########################################################
        ###### The user clicked points need subdivision ########
        newer_edges = []
        unchanged_edges = []
        
        bisect_eds = []
        bisect_pts = []
        for i, edge in enumerate(new_edges):
            if i in self.new_ed_face_map:
                #print('%i is in the new ed face map' % i)
                face_ind = self.new_ed_face_map[i]
                #print('edge %i is cross face %i' % (i, face_ind))
                if face_ind not in self.face_groups:
                    print('unfortunately, it is not in the face groups')
                    unchanged_edges += [edge]
                    continue
                #these are the user polyine vertex indices
                vert_inds = self.face_groups[face_ind]
                
                if len(vert_inds):
                    if len(vert_inds) > 1:
                        print('there are %i user drawn poly points on the face' % len(vert_inds))
                    
                    bisect_eds += [edge]
                    bisect_pts += [self.cut_pts[vert_inds[0]]]  #TODO, this only allows for a single point per face
                    
                    #geom =  bmesh.ops.bisect_edges(self.bme, edges = [edge],cuts = len(vert_inds),edge_percents = {})
                    #new_bmverts = [ele for ele in geom['geom_split'] if isinstance(ele, bmesh.types.BMVert)]
                    #newer_edges += [ele for ele in geom['geom_split'] if isinstance(ele, bmesh.types.BMEdge)]
                    
                    #if len(vert_inds) == 1:
                    #    new_bmverts[0].co = self.cut_pts[vert_inds[0]]
        
                    #self.bme.verts.ensure_lookup_table()
                    #self.bme.edges.ensure_lookup_table()
                else:
                    print('#################################')
                    print('there are not user drawn points...what do we do!?')
                    print('so this may not be gettings split')
                    print('#################################')
                    
            else:
                #print('%i edge crosses a face in the walking algo, unchanged' % i)
                unchanged_edges += [edge]
        
        geom =  bmesh.ops.bisect_edges(self.bme, edges = bisect_eds,cuts = len(vert_inds),edge_percents = {})
        new_bmverts = [ele for ele in geom['geom_split'] if isinstance(ele, bmesh.types.BMVert)]
        newer_edges += [ele for ele in geom['geom_split'] if isinstance(ele, bmesh.types.BMEdge)]
        
        print('Len of new bmverts %i and len of expected verts %i' % (len(bisect_pts), len(new_bmverts)))
        for v, loc in zip(new_bmverts, bisect_pts):
            v.co = loc
                    
        finish = time.time()
        print('Took %f seconds to bisect %i multipoint edges' % ((finish-start), len(newer_edges)))
        print('Leaving %i unchanged edges' % len(unchanged_edges))
        start = finish
        
        for ed in new_edges:
            ed.select_set(True)
            
        for ed in newer_edges:
            ed.select_set(True)
        
        
        
        face_boundary = set()
        for ed in new_edges:
            face_boundary.update(list(ed.link_faces))
        for ed in newer_edges:
            face_boundary.update(list(ed.link_faces))
        
        self.bme.verts.ensure_lookup_table()
        self.bme.edges.ensure_lookup_table()
        self.bme.faces.ensure_lookup_table()
        
        self.perimeter_edges = list(set(new_edges) | set(newer_edges))        
        finish = time.time()
        #print('took %f seconds' % (finish-start))
        self.split = True

    def confirm_cut_to_mesh_no_ops(self):
        
        if len(self.bad_segments): return  #can't do this with bad segments!!
        
        if self.split: return #already split! no going back
    
        for v in self.bme.verts:
            v.select_set(False)
        for ed in self.bme.edges:
            ed.select_set(False)  
        for f in self.bme.faces:
            f.select_set(False)
                
        start = time.time()


        self.perimeter_edges = []
            
        #Create new vetices
        new_vert_ed_map = {}
        new_bmverts = [self.bme.verts.new(co) for co in self.new_cos]
        for bmed, bmvert in zip(self.ed_map, new_bmverts):
            if bmed not in new_vert_ed_map:
                new_vert_ed_map[bmed] = [bmvert]
            else:
                print('Ed crossed multiple times.')
                new_vert_ed_map[bmed] += [bmvert]
            
        
        print('took %f seconds to create %i new verts and map them to edges' % (time.time()-start, len(new_bmverts)))
        finish = time.time()
        
        #SPLIT ALL THE CROSSED FACES
        fast_ed_map = set(self.ed_map)
        del_faces = []
        new_faces = []
        
        print('len of face chain %i' % len(self.face_chain))
        errors = []
        for bmface in self.face_chain:
            

            eds_crossed = [ed for ed in bmface.edges if ed in fast_ed_map]
            
            #scenario 1, it was simply crossed by cut plane
            #no user clicked points
            if bmface.index not in self.face_groups and len(eds_crossed) == 2:            
                if any([len(new_vert_ed_map[ed]) > 1 for ed in eds_crossed]):
                    print('2 edges with some double crossed! skipping this face')
                    errors += [(bmface, 'DOUBLE CROSS')]
                    continue
                   
                ed0 = min(eds_crossed, key = self.ed_map.index)
                
                if ed0 == eds_crossed[0]: 
                    ed1 = eds_crossed[1] 
                else: 
                    ed1 = eds_crossed[0]
                
                for v in ed0.verts:
                    new_face_verts = [new_vert_ed_map[ed0][0]]
                    next_v = ed0.other_vert(v)
                    next_ed = [ed for ed in next_v.link_edges if ed in bmface.edges and ed != ed0][0] #TODO, what if None?
                    
                    iters = 0  #safety for now, no 100 vert NGONS allowed!
                    while next_ed != None and iters < 100:
                        iters += 1
                        if next_ed == ed1:
                            new_face_verts += [next_v]
                            new_face_verts += [new_vert_ed_map[ed1][0]]
                            break
                        else:
                            new_face_verts += [next_v]
                            next_v = next_ed.other_vert(next_v)
                            next_ed = [ed for ed in next_v.link_edges if ed in bmface.edges and ed != next_ed][0]
                        
                    
                    new_faces += [self.bme.faces.new(tuple(new_face_verts))]
                
                #put the new edge into perimeter edges
                for ed in new_faces[-1].edges:
                    if new_vert_ed_map[ed0][0] in ed.verts and new_vert_ed_map[ed1][0] in ed.verts:
                        self.perimeter_edges += [ed]
                
                del_faces += [bmface]
            
            elif bmface.index in self.face_groups and len(eds_crossed) == 2:
                
                sorted_eds_crossed = sorted(eds_crossed, key = self.ed_map.index)
                ed0 = sorted_eds_crossed[0]
                ed1 = sorted_eds_crossed[1]
                
                
                #make the new verts corresponding to the user click on bmface
                inner_vert_cos = [self.cut_pts[i] for i in self.face_groups[bmface.index]]
                inner_verts = [self.bme.verts.new(co) for co in inner_vert_cos]
                
                if self.ed_map.index(ed0) != 0:
                    inner_verts.reverse()
                    
                for v in ed0.verts:
                    new_face_verts = inner_verts + [new_vert_ed_map[ed0][0]]
                    next_v = ed0.other_vert(v)
                    next_ed = [ed for ed in next_v.link_edges if ed in bmface.edges and ed != ed0][0]
                    
                    iters = 0  #safety for now, no 100 vert NGONS allowed!
                    while next_ed != None and iters < 100:
                        iters += 1
                        if next_ed == ed1:
                            new_face_verts += [next_v]
                            new_face_verts += [new_vert_ed_map[ed1][0]]
                            
                            break
                        else:
                            new_face_verts += [next_v]
                            next_v = next_ed.other_vert(next_v)
                            next_ed = [ed for ed in next_v.link_edges if ed in bmface.edges and ed != next_ed][0]
                        
                    
                    new_faces += [self.bme.faces.new(tuple(new_face_verts))]
                    
                vert_chain = [new_vert_ed_map[ed1][0]] + inner_verts + [new_vert_ed_map[ed0][0]]
                
                eds = new_faces[-1].edges
                for i, v in enumerate(vert_chain):
                    if i == len(vert_chain) -1: continue
                    for ed in eds:
                        if ed.other_vert(v) == vert_chain[i+1]:
                            self.perimeter_edges += [ed]
                            break
                
                del_faces += [bmface]
                    
            elif bmface.index in self.face_groups and len(eds_crossed) == 1:
                
                print('ONE EDGE CROSSED TWICE?')
                
                ed0 = eds_crossed[0]
                
                #make the new verts corresponding to the user click on bmface
                inner_vert_cos = [self.cut_pts[i] for i in self.face_groups[bmface.index]]
                inner_verts = [self.bme.verts.new(co) for co in inner_vert_cos]
                
                #A new face made entirely out of new verts
                if eds_crossed.index(ed0) == 0:
                    print('first multi face reverse')
                    inner_verts.reverse()
                    new_face_verts = [new_vert_ed_map[ed0][1]] + inner_verts + [new_vert_ed_map[ed0][0]]
                    loc = new_vert_ed_map[ed0][0].co
                else:
                    new_face_verts = [new_vert_ed_map[ed0][0]] + inner_verts + [new_vert_ed_map[ed0][1]]
                    loc = new_vert_ed_map[ed0][1].co
                
                vert_chain = new_face_verts  #hang on to these for later
                new_faces += [self.bme.faces.new(tuple(new_face_verts))]
        
                #The old face, with new verts inserted
                v_next = min(ed0.verts, key = lambda x: (x.co - loc).length)
                v_end = ed0.other_vert(v_next)
                
                next_ed = [ed for ed in v_next.link_edges if ed in bmface.edges and ed != ed0][0]
                iters = 0
                while next_ed != None and iters < 100:
                    iters += 1
                    if next_ed == ed0:
                        new_face_verts += [v_end]
                        break
                    
                    else:
                        new_face_verts += [v_next]
                        v_next = next_ed.other_vert(v_next)
                        next_ed = [ed for ed in v_next.link_edges if ed in bmface.edges and ed != next_ed][0]
                    
                if iters > 10:
                    print('This may have iterated out.  %i' % iters)
                    errors += [(bmface, 'TWO CROSS AND ITERATIONS')]
                    
                new_faces += [self.bme.faces.new(tuple(new_face_verts))]
                
                eds = new_faces[-1].edges
                for i, v in enumerate(vert_chain):
                    if i == len(vert_chain) - 1: continue
                    for ed in eds:
                        if ed.other_vert(v) == vert_chain[i+1]:
                            self.perimeter_edges += [ed]
                            break
                
                del_faces += [bmface]
                
            else:
                print('\n')
                print('THIS SCENARIO MAY NOT  ACCOUNTED FOR YET')
  
                print('There are %i eds crossed' % len(eds_crossed))
                print('BMFace index %i' % bmface.index)
                print('These are the face groups')
                print(self.face_groups)
                
                if bmface.index in self.face_groups:
                    print('cant cross face twice and have user point on it...ignoring user clicked points')
                    errors += [(bmface, 'CLICK AND DOUBLE CROSS')]
                    continue
                
                sorted_eds_crossed = sorted(eds_crossed, key = self.ed_map.index)   
                ed0 = sorted_eds_crossed[0]
                ed1 = sorted_eds_crossed[1]
                ed2 = sorted_eds_crossed[2]
                corners = set([v for v in bmface.verts])
                
                if len(new_vert_ed_map[ed0]) == 2:
                    vs = new_vert_ed_map[ed0]
                    vs.reverse()
                    new_vert_ed_map[ed0] = vs
                    #change order
                    ed0, ed1, ed2 = ed2, ed0, ed1
                
                
                for v in ed0.verts:
                    corners.remove(v)
                    new_face_verts = [new_vert_ed_map[ed0][0], v]
                    next_ed = [ed for ed in v.link_edges if ed in bmface.edges and ed != ed0][0]
                    v_next = v
                    while next_ed:
                        
                        if next_ed in sorted_eds_crossed:
                            if len(new_vert_ed_map[next_ed]) > 1:
                                loc = v_next.co
                                #choose the intersection closest to the corner vertex
                                v_last = min(new_vert_ed_map[next_ed], key = lambda x: (x.co - loc).length)
                                new_face_verts += [v_last]
                            else:
                                new_face_verts += [new_vert_ed_map[next_ed][0]]
                                
                                if next_ed == ed1:
                                    print('THIS IS THE PROBLEM!  ALREDY DONE')
                                
                                v_next = next_ed.other_vert(v_next)
                                next_ed = [ed for ed in v_next.link_edges if ed in bmface.edges and ed != next_ed][0]
                                while next_ed != ed1:
                                    v_next = next_ed.other_vert(v_next)
                                    next_ed = [ed for ed in v_next.link_edges if ed in bmface.edges and ed != next_ed][0]        
                    
                                vs = sorted(new_vert_ed_map[ed1], key = lambda x: (x.co - v_next.co).length)
                                new_face_verts += vs
                            
                            if len(new_face_verts) != len(set(new_face_verts)):
                                print("ERRROR, multiple verts")
                                print(new_face_verts)
                                
                                print('There are %i verts in vs %i' % (len(vs),bmface.index))
                                print(vs)
                                
                                print('attempting a dumb hack')
                                new_face_verts.pop()
                                errors += [(bmface, 'MULTIPLE VERTS')]
                                
                                        
                            new_faces += [self.bme.faces.new(tuple(new_face_verts))]
                            break
                                
                        v_next = next_ed.other_vert(v_next)
                        next_ed = [ed for ed in v_next.link_edges if ed in bmface.edges and ed != next_ed][0]
                        new_face_verts += [v_next]
                        corners.remove(v_next)
                
                for ed in new_faces[-1].edges:
                    if ed.other_vert(new_vert_ed_map[ed0][0]) == new_vert_ed_map[ed1][0]:
                        self.perimeter_edges += [ed]
                        print('succesfully added edge?')
                        break
                #final corner
                print('There shouldnt be too many left in corners %i' % len(corners))
                v0 = [v for v in corners if v in ed2.verts][0]
                vf = min(new_vert_ed_map[ed1], key = lambda x: (x.co - v0.co).length)
                new_face_verts = [new_vert_ed_map[ed2][0], v0, vf]
                new_faces += [self.bme.faces.new(tuple(new_face_verts))]
                
                for ed in new_faces[-1].edges:
                    if ed.other_vert(new_vert_ed_map[ed1][1]) == new_vert_ed_map[ed2][0]:
                        self.perimeter_edges += [ed]
                        print('succesffully added edge?')
                        break
                
                del_faces += [bmface]
                

        print('took %f seconds to split the faces' % (time.time() - finish))        
        finish = time.time()
                
 
        self.bme.verts.ensure_lookup_table()
        self.bme.edges.ensure_lookup_table()
        self.bme.faces.ensure_lookup_table()
        
        for bmface, msg in errors:
            print('Error on this face %i' % bmface.index)
            bmface.select_set(True)
            
        bmesh.ops.delete(self.bme, geom = del_faces, context = 5) ## This causes error!!!!
        
        self.bme.verts.ensure_lookup_table()
        self.bme.edges.ensure_lookup_table()
        self.bme.faces.ensure_lookup_table()
        
        self.bme.normal_update()
        
        #normal est
        to_test = set(new_faces)
        iters = 0
        while len(to_test) and iters < 10:
            iters += 1
            print('test round %i' % iters)
            to_remove = []
            for test_f in to_test:
                link_faces = []
                for ed in test_f.edges:
                    if len(ed.link_faces) > 1:
                        for f in ed.link_faces:
                            if f not in to_test:
                                link_faces += [f]
                if len(link_faces)== 0:    
                    continue
                
                if test_f.normal.dot(link_faces[0].normal) < 0:
                    print('NEEDS FLIP')
                    test_f.normal_flip()   
                else:
                    print('DOESNT NEED FLIP')
                to_remove += [test_f]
            to_test.difference_update(to_remove)
        #bmesh.ops.recalc_face_normals(self.bme, faces = new_faces)

        self.bme.verts.ensure_lookup_table()
        self.bme.edges.ensure_lookup_table()
        self.bme.faces.ensure_lookup_table()
            
        #ngons = [f for f in new_faces if len(f.verts) > 4]
        #bmesh.ops.triangulate(self.bme, faces = ngons)
        
        for ed in self.perimeter_edges:
            ed.select_set(True)
            
                  
        finish = time.time()
        print('took %f seconds' % (finish-start))
        self.split = True
         
    def calc_ed_pcts(self):
        '''
        not used until bmesh.ops uses the percentage index
        '''
        if not len(self.ed_map) and len(self.new_cos): return
        
        for v, ed in zip(self.new_cos, self.ed_map):
            
            v0 = ed.verts[0].co
            v1 = ed.verts[1].co
            
            ed_vec = v1 - v0
            L = ed_vec.length
            
            cut_vec = v - v0
            l = cut_vec.length
            
            pct = l/L
     
    def preview_mesh(self, context):
        
        self.find_select_inner_faces()
        context.tool_settings.mesh_select_mode = (False, True, False)
        self.bme.to_mesh(self.cut_ob.data)
        
        #store the cut!
        cut_bme = bmesh.new()
        cut_me = bpy.data.meshes.new('polyknife_stroke')
        cut_ob = bpy.data.objects.new('polyknife_stroke', cut_me)
        cut_ob.hide = True
        bmvs = [cut_bme.verts.new(co) for co in self.cut_pts]
        for v0, v1 in zip(bmvs[:-1], bmvs[1:]):
            cut_bme.edges.new((v0,v1))
        
        if self.cyclic:
            cut_bme.edges.new((bmvs[-1], bmvs[0])) 
        cut_bme.to_mesh(cut_me)
        context.scene.objects.link(cut_ob)
        cut_ob.show_x_ray = True
           
    def split_geometry(self, context, mode = 'DUPLICATE'):
        '''
        mode:  Enum in {'KNIFE','DUPLICATE', 'DELETE', 'SPLIT', 'SEPARATE'}
        '''
        #if not (self.split and self.face_seed): return
        
        start = time.time()
        self.find_select_inner_faces()
        
        self.bme.verts.ensure_lookup_table()
        self.bme.edges.ensure_lookup_table()
        self.bme.faces.ensure_lookup_table()
            
        #bmesh.ops.recalc_face_normals(self.bme, faces = self.bme.faces)
        #bmesh.ops.recalc_face_normals(self.bme, faces = self.bme.faces)
        
        if mode == 'KNIFE':
            '''
            this mode just confirms the new cut edges to the mesh
            does not separate them
            '''
            
            self.bme.to_mesh(self.cut_ob.data)
            
        if mode == 'SEPARATE':
            '''
            separates the selected portion into a new object
            leaving hole in original object
            this is destructive
            '''
            if not (self.split and self.face_seed): return
            output_bme = bmesh.new()
            
            verts = set()
            vert_lookup = {}
            for f in self.inner_faces:
                verts.update([v for v in f.verts])
                
            vert_list = list(verts)
            new_bmverts = []
            for i, v in enumerate(vert_list):
                vert_lookup[v.index] = i
                new_bmverts += [output_bme.verts.new(v.co)]
                
            for f in self.inner_faces:
                f_ind_tuple = [vert_lookup[v.index] for v in f.verts]
                f_vert_tuple = [new_bmverts[i] for i in f_ind_tuple]
                output_bme.faces.new(tuple(f_vert_tuple))
            
            new_data = bpy.data.meshes.new(self.cut_ob.name + ' trimmed') 
            new_ob =   bpy.data.objects.new(self.cut_ob.name + ' trimmed', new_data)
            new_ob.matrix_world = self.cut_ob.matrix_world
            output_bme.to_mesh(new_data)
            context.scene.objects.link(new_ob)
            
            
            # Get material
            mat = bpy.data.materials.get("Polytrim Material")
            if mat is None:
                # create material
                mat = bpy.data.materials.new(name="Polytrim Material")
                mat.diffuse_color = Color((0.1, .5, .8))
            # Assign it to object
            if new_ob.data.materials:
                # assign to 1st material slot
                new_ob.data.materials[0] = mat
            else:
                # no slots
                new_ob.data.materials.append(mat)
            
            bmesh.ops.delete(self.bme, geom = self.inner_faces, context = 5)
            self.bme.to_mesh(self.cut_ob.data)
            
        
        
        elif mode == 'DELETE':
            '''
            deletes the selected region of mesh
            This is destructive method
            '''
            print('DELETING THE INNER GEOM')
            self.find_select_inner_faces()
            
            gdict = bmesh.ops.split_edges(self.bme, edges = self.perimeter_edges, verts = [], use_verts = False) 
            #this dictionary is bad...just empy stuff
            
            self.bme.verts.ensure_lookup_table()
            self.bme.edges.ensure_lookup_table()
            self.bme.faces.ensure_lookup_table()
            
            
            #bmesh.ops.delete(self.bme, geom = self.inner_faces, context = 5)
            bmesh.ops.delete(self.bme, geom = self.inner_faces, context = 5)
            
            self.bme.to_mesh(self.cut_ob.data)
            self.bme.free()
            
            
        elif mode == 'SPLIT':
            '''
            splits the mesh, leaving 2 separate pieces in the original object
            This is destructive method
            '''
            if not self.split: return
            #print('There are %i perimeter edges' % len(self.perimeter_edges))
            
            #old_eds = set([e for e in self.bme.edges])    
            #gdict = bmesh.ops.split_edges(self.bme, edges = self.perimeter_edges, verts = [], use_verts = False) 
            #this dictionary is bad...just empy stuff
            
            #self.bme.verts.ensure_lookup_table()
            #self.bme.edges.ensure_lookup_table()
            #self.bme.faces.ensure_lookup_table()
            
            #current_edges = set([e for e in self.bme.edges])           
            #new_edges = current_edges - old_eds
            #for ed in new_edges:
            #    ed.select_set(True)
            #print('There are %i new edges' % len(new_edges))
            
            self.bme.to_mesh(self.cut_ob.data)
            
            
        elif mode == 'DUPLICATE':
            '''
            creates a new object with the selected portion
            of original but leavs the original object un-touched
            '''
            if not (self.split and self.face_seed): return
            output_bme = bmesh.new()
            
            verts = set()
            vert_lookup = {}
            for f in self.inner_faces:
                verts.update([v for v in f.verts])
                
            vert_list = list(verts)
            new_bmverts = []
            for i, v in enumerate(vert_list):
                vert_lookup[v.index] = i
                new_bmverts += [output_bme.verts.new(v.co)]
                
            for f in self.inner_faces:
                f_ind_tuple = [vert_lookup[v.index] for v in f.verts]
                f_vert_tuple = [new_bmverts[i] for i in f_ind_tuple]
                output_bme.faces.new(tuple(f_vert_tuple))
            
            new_data = bpy.data.meshes.new(self.cut_ob.name + ' trimmed') 
            new_ob =   bpy.data.objects.new(self.cut_ob.name + ' trimmed', new_data)
            new_ob.matrix_world = self.cut_ob.matrix_world
            output_bme.to_mesh(new_data)
            context.scene.objects.link(new_ob)
            
            # Get material
            mat = bpy.data.materials.get("Polytrim Material")
            if mat is None:
                # create material
                mat = bpy.data.materials.new(name="Polytrim Material")
                mat.diffuse_color = Color((0.1, .5, .8))
            # Assign it to object
            if new_ob.data.materials:
                # assign to 1st material slot
                new_ob.data.materials[0] = mat
            else:
                # no slots
                new_ob.data.materials.append(mat)
            
            #bmesh.ops.delete(self.bme, geom = self.inner_faces, context = 5)
            #self.bme.to_mesh(self.cut_ob.data)
            self.bme.free()
            
            
        #store the cut as an object
        cut_bme = bmesh.new()
        cut_me = bpy.data.meshes.new('polyknife_stroke')
        cut_ob = bpy.data.objects.new('polyknife_stroke', cut_me)
        
        bmvs = [cut_bme.verts.new(co) for co in self.cut_pts]
        for v0, v1 in zip(bmvs[:-1], bmvs[1:]):
            cut_bme.edges.new((v0,v1))
        
        if self.cyclic:
            cut_bme.edges.new((bmvs[-1], bmvs[0])) 
        cut_bme.to_mesh(cut_me)
        context.scene.objects.link(cut_ob)
        cut_ob.show_x_ray = True

    def find_select_inner_faces(self):
        if not self.face_seed: return
        if len(self.bad_segments): return
        f0 = self.face_seed
        #inner_faces = flood_selection_by_verts(self.bme, set(), f0, max_iters=1000)
        inner_faces = flood_selection_edge_loop(self.bme, self.perimeter_edges, f0, max_iters = 20000)
        
        if len(inner_faces) == len(self.bme.faces):
            print('region growing selected entire mesh!')
            self.inner_faces = []
        else:
            self.inner_faces = list(inner_faces)
        
        for f in self.bme.faces:
            f.select_set(False)
        #for f in inner_faces:
        #    f.select_set(True)
        
        print('Found %i faces in the region' % len(inner_faces))
     
    def replace_segment(self,start,end,new_locs):
        #http://stackoverflow.com/questions/497426/deleting-multiple-elements-from-a-list
        print('replace')
        return

    def snap_poly_line(self):
        '''
        only needed if processing an outside mesh
        '''
        locs = []
        self.face_map = []
        #self.normals = [] for now, leave normals from view direction
        self.face_changes = []
        self.face_groups = dict()
        
        mx = self.cut_ob.matrix_world
        imx = mx.inverted()
        
        last_face_ind = None
        for i, v in enumerate(self.pts):
            if bversion() < '002.077.000':
                loc, no, ind, d = self.bvh.find(imx * v)
            else:
                loc, no, ind, d = self.bvh.find_nearest(imx * v)
                
            self.face_map.append(ind)
            locs.append(loc)
            
            if i == 0:
                last_face_ind = ind
                group = [i]
                print('first face group index')
                print((ind,group))
                
            if ind != last_face_ind: #we have found a new face
                self.face_changes.append(i-1)
                
                if last_face_ind not in self.face_groups: #previous face has not been mapped before
                    self.face_groups[last_face_ind] = group
                    last_face_ind = ind
                    group = [i]
                else:
                    print('group already in dictionary')
                    exising_group = self.face_groups[last_face_ind]
                    if 0 not in exising_group:
                        print('LOOKS LIKE WE CLICKED SAME FACE MULTIPLE TIMES')
                        print('YOUR PROGRAMMER IS NOT SMART ENOUGH FOR THIS')
                        #TODO....GENERATE SOME ERROR
                        #TODO....REMOVE SELF INTERSECTIONS IN ORIGINAL PATH
                        
                    self.face_groups[last_face_ind] = group + exising_group #we have wrapped, add this group to the old
            
            else:
                if i != 0:
                    group += [i]
            #double check for the last point
            if i == len(self.pts) - 1:  #
                if ind != self.face_map[0]:  #we didn't click on the same face we started on
                    
                    if self.cyclic:
                        self.face_changes.append(i)
                        
                    if ind not in self.face_groups:
                        print('final group not added to dictionary yet')
                        print((ind, group))
                        self.face_groups[ind] = group
                    
                    else:
                        print('group already in dictionary')
                        exising_group = self.face_groups[ind]
                        if 0 not in exising_group:
                            print('LOOKS LIKE WE CROSSED SAME FACE MULTIPLE TIMES')
                            print('YOUR PROGRAMMER IS NOT SMART ENOUGH FOR THIS')
                        self.face_groups[ind] = group + exising_group
                        
                else:
                    print('group already in dictionary')
                    exising_group = self.face_groups[ind]
                    if 0 not in exising_group:
                        print('LOOKS LIKE WE CROSSED SAME FACE MULTIPLE TIMES')
                        print('YOUR PROGRAMMER IS NOT SMART ENOUGH FOR THIS')
                    self.face_groups[ind] = group + exising_group
                              
        self.cut_pts = locs
        
        #clean up face groups if necessary
        #TODO, get smarter about not adding in these
        if not self.cyclic:
            if self.start_edge:
                s_ind = self.start_edge.link_faces[0].index
                if s_ind in self.face_groups:
                    v_group = self.face_groups[s_ind]
                    if len(v_group) == 1:
                        print('remove first face from face groups')
                        del self.face_groups[s_ind]
                    elif len(v_group) > 1:
                        print('remove first vert from first face group')
                        v_group.pop(0)
                        self.face_groups[s_ind] = v_group
            if self.end_edge:
                e_ind = self.end_edge.link_faces[0].index        
                if e_ind in self.face_groups:
                    v_group = self.face_groups[e_ind]
                    if len(v_group) == 1:
                        print('remove last face from face groups')
                        del self.face_groups[e_ind]
                    elif len(v_group) > 1:
                        print('remove last vert from last face group')
                        v_group.pop()
                        self.face_groups[e_ind] = v_group              
   
   
    ## ****** DRAWING/UI *****

    ## 2D drawing
    def draw(self,context): 
        
        #draw a dot over the hovered vertex
        if self.hovered[0] in {'NON_MAN_ED', 'NON_MAN_VERT'}:
            ed, pt = self.hovered[1]
            common_drawing.draw_3d_points(context,[pt], 6, color = (.3,1,.3,1))
                    
        ## If there are no input points, nothing else needs be drawn
        if len(self.pts) == 0: return
        
        #if self.cyclic and len(self.pts):
        #    common_drawing.draw_polyline_from_3dpoints(context, self.pts + [self.pts[0]], (.1,.2,1,.8), 2, 'GL_LINE_STRIP')
        
        #else:
        #    common_drawing.draw_polyline_from_3dpoints(context, self.pts, (.1,.2,1,.8), 2, 'GL_LINE')
        
        #if self.ui_type != 'DENSE_POLY':    
        #    common_drawing.draw_3d_points(context,self.pts, 3)
        #    common_drawing.draw_3d_points(context,[self.pts[0]], 8, color = (1,1,0,1))
            
        #else:
        #    common_drawing.draw_3d_points(context,self.pts, 4, color = (1,1,1,1)) 
        #    common_drawing.draw_3d_points(context,[self.pts[0]], 4, color = (1,1,0,1))
        

        
        #draw a dot of sepecfic color representing the user selected input point
        if self.selected != -1 and len(self.pts) >= self.selected + 1:
            common_drawing.draw_3d_points(context,[self.pts[self.selected]], 8, color = (0,1,1,1))
        
        #pre-highlight the point under the mouse        
        if self.hovered[0] == 'POINT':
            common_drawing.draw_3d_points(context,[self.pts[self.hovered[1]]], 8, color = (0,1,0,1))
     
        #draw the silly green insertion line when possible to cut a new input point into an existing cut segment
        elif self.hovered[0] == 'EDGE':
            loc3d_reg2D = view3d_utils.location_3d_to_region_2d
            a = loc3d_reg2D(context.region, context.space_data.region_3d, self.pts[self.hovered[1]])
            next = (self.hovered[1] + 1) % len(self.pts)
            b = loc3d_reg2D(context.region, context.space_data.region_3d, self.pts[next])
            common_drawing.draw_polyline_from_points(context, [a,self.mouse, b], (0,.2,.2,.5), 2,"GL_LINE_STRIP")  
        
        #draw the vertices of the seed face
        if self.face_seed:
            #TODO direct bmesh face drawing util
            vs = self.face_seed.verts
            common_drawing.draw_3d_points(context,[self.cut_ob.matrix_world * v.co for v in vs], 4, color = (1,1,.1,1))
            
            #if len(self.prev_region):
            #    common_drawing.draw_3d_points(context,[self.cut_ob.matrix_world * v for v in self.prev_region], 2, color = (1,1,.1,.2))
            
        #if len(self.new_cos):
        #    if self.split: 
        #        color = (.1, .1, .8, 1)
        #    else: 
        #        color = (.2,.5,.2,1)
        #    common_drawing.draw_3d_points(context,[self.cut_ob.matrix_world * v for v in self.new_cos], 6, color = color)
        
        #draw any bad segments in red
        print()
        print("face changes:", self.face_changes)
        if len(self.bad_segments):
            for ind in self.bad_segments:
                m = self.face_changes.index(ind)
                print("bad segment starting at index ",m)
                m_p1 = (m + 1) % (len(self.face_changes) + 1)
                ind_p1 = self.face_changes[m_p1]
                common_drawing.draw_polyline_from_3dpoints(context, [self.pts[ind], self.pts[ind_p1]], (1,.1,.1,1), 4, 'GL_LINE')

    ## 3D drawing
    def draw3d(self,context):
        #ADAPTED FROM POLYSTRIPS John Denning @CGCookie and Taylor University
        if len(self.pts) == 0: return
        
        region,r3d = context.region,context.space_data.region_3d
        view_dir = r3d.view_rotation * Vector((0,0,-1))
        view_loc = r3d.view_location - view_dir * r3d.view_distance
        if r3d.view_perspective == 'ORTHO': view_loc -= view_dir * 1000.0
        
        bgl.glEnable(bgl.GL_POINT_SMOOTH)
        bgl.glDepthRange(0.0, 1.0)
        bgl.glEnable(bgl.GL_DEPTH_TEST)
        
        
        
        def set_depthrange(near=0.0, far=1.0, points=None):
            if points and len(points) and view_loc:
                d2 = min((view_loc-p).length_squared for p in points)
                d = math.sqrt(d2)
                d2 /= 10.0
                near = near / d2
                far = 1.0 - ((1.0 - far) / d2)
            if r3d.view_perspective == 'ORTHO':
                far *= 0.9999
            near = max(0.0, min(1.0, near))
            far = max(near, min(1.0, far))
            bgl.glDepthRange(near, far)
            #bgl.glDepthRange(0.0, 0.5)
            
        # function for drawing points
        def draw3d_points(context, points, color, size):
            #if type(points) is types.GeneratorType:
            #    points = list(points)
            if len(points) == 0: return
            bgl.glColor4f(*color)
            bgl.glPointSize(size)
            set_depthrange(0.0, 0.997, points)
            bgl.glBegin(bgl.GL_POINTS)
            for coord in points: bgl.glVertex3f(*coord)
            bgl.glEnd()
            bgl.glPointSize(1.0)
            
        # function for drawing polylines.
        def draw3d_polyline(context, points, color, thickness, LINE_TYPE, zfar=0.997):
            
            if len(points) == 0: return
            # if type(points) is types.GeneratorType:
            #     points = list(points)
            if LINE_TYPE == "GL_LINE_STIPPLE":
                bgl.glLineStipple(4, 0x5555)  #play with this later
                bgl.glEnable(bgl.GL_LINE_STIPPLE)  
            bgl.glEnable(bgl.GL_BLEND)
            bgl.glColor4f(*color)
            bgl.glLineWidth(thickness)
            set_depthrange(0.0, zfar, points)
            bgl.glBegin(bgl.GL_LINE_STRIP)
            for coord in points: bgl.glVertex3f(*coord)
            bgl.glEnd()
            bgl.glLineWidth(1)
            if LINE_TYPE == "GL_LINE_STIPPLE":
                bgl.glDisable(bgl.GL_LINE_STIPPLE)
                bgl.glEnable(bgl.GL_BLEND)  # back to uninterrupted lines
                
        bgl.glLineWidth(1)
        bgl.glDepthRange(0.0, 1.0)
        
        
        
        # draws the polylines for after 'c' has been pressed/ after make_cut.
        if len(self.new_cos):
            if self.split: 
                color = (.1, .1, .8, 1)
            else: 
                color = (.2,.5,.2,1)
            draw3d_polyline(context,[self.cut_ob.matrix_world * v for v in self.new_cos], color, 5, 'GL_LINE_STRIP')
            
        
        # draw the polylines for before 'c' has been pressed/before make_cut
        else:
            if self.cyclic and len(self.pts):
                draw3d_polyline(context, self.pts + [self.pts[0]],  (.1,.2,1,.8), 2, 'GL_LINE_STRIP' )
            else:
                draw3d_polyline(context, self.pts,  (.1,.2,1,.8),2, 'GL_LINE' )
        
        # draw points
        draw3d_points(context, [self.pts[0]], (1,.8,.2,1), 10)
        if len(self.pts) > 1:
            draw3d_points(context, self.pts[1:], (.2, .2, .8, 1), 6)
                
        bgl.glLineWidth(1)
        bgl.glDepthRange(0.0, 1.0)
        
        
        
        
class PolyCutPoint(object):
    
    def __init__(self,co):
        self.co = co
        
        self.no = None
        self.face = None
        self.face_region = set()
        
    def find_closest_non_manifold(self):
        return None
    
class NonManifoldEndpoint(object):
    
    def __init__(self,co, ed):
        if len(ed.link_faces) != 1:
            return None
        
        self.co = co
        self.ed = ed
        self.face = ed.link_faces[0]
        
    
        
        
        
        