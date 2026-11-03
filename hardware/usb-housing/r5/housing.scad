// R5: two straight-nesting parts, four recessed M3 screws. Native units mm.
part="assembled"; emit_parameters=false;
insert_od=4.6; insert_l=5.7; insert_pilot=4.0;
$fn=64;
height=20; floor_t=2; roof_t=2; seam=13.5;
body=[-9.2,74.8,-3.7,32.2]; inside=[-7.4,73.0,-1.9,30.4];
axes=[[-5,1.6],[-5,28],[70.4,1.6],[70.4,28]]; boss_r=4.2;
head_d=6.8; head_land=16.5; clearance_d=4.0;
insert_top=13.5; blind_bottom=6.8;
lip_t=1.2; lip_depth=2.0; lip_clearance=.4; lip_chamfer=.5;
lower_pad=6.88; upper_pad=9.0;
mounts=[[3.549904,3.599942],[61.399928,3.599942],[3.599942,26.450036],[61.399928,26.500074]];
// No floor vents: dedicated low side inlets remain open on a flat desk.
inlet_x=[for(x=[14:6:56])x]; inlet_w=3; inlet_h=2.4; inlet_bottom=2.8;
outlet_x=[for(x=[16:6:52])x]; outlet_w=3; outlet_l=13;
brand_asset="../../../site/assets/yoke-mark.svg";

if(emit_parameters){
 echo("R5PARAM","envelope_mm",[body[1]-body[0],body[3]-body[2],height]);
 echo("R5PARAM","body_bounds_xy_mm",body);
 echo("R5PARAM","inside_bounds_xy_mm",inside);
 echo("R5PARAM","screw_axes_mm",axes);
 echo("R5PARAM","screw_length_mm",8);
 echo("R5PARAM","boss_diameter_mm",2*boss_r);
 echo("R5PARAM","head_well_diameter_mm",head_d);
 echo("R5PARAM","shank_passage_width_mm",clearance_d);
 echo("R5PARAM","head_bearing_z_mm",head_land);
 echo("R5PARAM","head_recess_mm",height-head_land-3);
 echo("R5PARAM","seam_z_mm",seam);
 echo("R5PARAM","insert_top_z_mm",insert_top);
 echo("R5PARAM","insert_bottom_z_mm",insert_top-insert_l);
 echo("R5PARAM","pilot_bottom_z_mm",blind_bottom);
 echo("R5PARAM","pilot_diameter_mm",insert_pilot);
 echo("R5PARAM","insert_diameter_mm",insert_od);
 echo("R5PARAM","insert_length_mm",insert_l);
 echo("R5PARAM","lip_thickness_mm",lip_t);
 echo("R5PARAM","lip_depth_mm",lip_depth);
 echo("R5PARAM","lip_per_side_clearance_mm",lip_clearance);
 echo("R5PARAM","lip_entry_chamfer_mm",lip_chamfer);
 echo("R5PARAM","pcb_supports_mm",mounts);
 echo("R5PARAM","pcb_lower_pad_z_mm",lower_pad);
 echo("R5PARAM","pcb_upper_capture_z_mm",upper_pad);
 echo("R5PARAM","side_inlets",[2,len(inlet_x),inlet_w,inlet_h,inlet_bottom]);
 echo("R5PARAM","side_inlet_area_mm2",2*len(inlet_x)*inlet_w*inlet_h);
 echo("R5PARAM","top_outlets",[len(outlet_x),outlet_w,outlet_l]);
 echo("R5PARAM","top_outlet_area_mm2",len(outlet_x)*outlet_w*outlet_l);
 echo("R5PARAM","floor_thickness_mm",floor_t);
 echo("R5PARAM","roof_thickness_mm",roof_t);
 echo("R5PARAM","lid_assembly_matrix",[[1,0,0,0],[0,-1,0,28.5],[0,0,-1,height],[0,0,0,1]]);
 echo("R5PARAM","board_matrix",[[1,0,0,0],[0,1,0,0],[0,0,1,8.5],[0,0,0,1]]);
}
module slab(b,z,h){translate([b[0],b[2],z])cube([b[1]-b[0],b[3]-b[2],h]);}
module walls(z,h){difference(){slab(body,z,h);slab(inside,z-.01,h+.02);}}
module ports(){
 // Cut through the internal nesting skirt too, not merely the outside wall.
 translate([body[0]-1,6.1,2.9])cube([-4.8-body[0]+1,17.9,13.3]);
 translate([6.8,body[2]-1,5.8])cube([53.7,.8-body[2]+1,9]);
 // Enlarged rear access window for the recessed microSD end.
 translate([65.3,6.1,2.9])cube([body[1]-65.3+1,17.6,13.3]);
}
module inlets(){for(x=inlet_x)for(y=[body[2]-1,inside[3]-.1])
 translate([x,y,inlet_bottom])cube([inlet_w,3,inlet_h]);}
module outlets(){for(x=outlet_x)translate([x,8,height-roof_t-.1])cube([outlet_w,outlet_l,roof_t+.2]);}
module locator(i){p=mounts[i];translate([p[0],p[1],lower_pad])intersection(){
 union(){cylinder(d=2.35,h=1.67);translate([0,0,1.67])cylinder(d1=2.35,d2=1.85,h=.25);}
 if(i==3)rotate([0,0,atan2(mounts[3][1]-mounts[0][1],mounts[3][0]-mounts[0][0])])translate([-.8,-2,0])cube([1.6,4,3]);
 else cylinder(d=3,h=3);
}}
module skirt_profile(){polygon([
 [lip_chamfer,seam-lip_depth],[lip_t,seam-lip_depth],
 [lip_t,height-roof_t+.02],[0,height-roof_t+.02],
 [0,seam-lip_depth+lip_chamfer]
]);}
module nesting_skirt(){difference(){union(){
 // One native 2D section per side avoids a coincident chamfer/root join.
 for(back=[false,true])translate([inside[0]+lip_clearance,back?inside[3]-lip_clearance:inside[2]+lip_clearance,0])
 scale([1,back?-1:1,1])rotate([90,0,90])linear_extrude(inside[1]-inside[0]-2*lip_clearance)skirt_profile();
 for(right=[false,true])translate([right?inside[1]-lip_clearance:inside[0]+lip_clearance,inside[3]-lip_clearance,0])
 scale([right?-1:1,1,1])rotate([90,0,0])linear_extrude(inside[3]-inside[2]-2*lip_clearance)skirt_profile();
 }
 // Leave the base's screw-boss cylinders unobstructed through the full drop.
 for(p=axes)translate([p[0],p[1],0])cylinder(r=boss_r+lip_clearance,h=seam+.01);
}}
module base(){difference(){union(){
 slab(body,0,floor_t);walls(0,seam);
 for(p=axes)translate([p[0],p[1],0])cylinder(r=boss_r,h=insert_top);
 for(p=mounts)translate([p[0],p[1],0])cylinder(d=4.6,h=lower_pad);
 locator(0);locator(3);
 }ports();inlets();
 for(p=axes)translate([p[0],p[1],blind_bottom]){
 cylinder(d=insert_pilot,h=insert_top-blind_bottom+.1);
 translate([0,0,insert_top-blind_bottom-.35])cylinder(d1=insert_pilot,d2=insert_od+.3,h=.36);
 }
 // Viewed from below, +screen-X is -assembly-X: the arrow still points to LAN.
 translate([27,15,-.01])linear_extrude(.41)mirror([1,0,0])text("R5 B  LAN >",size=2.4,font="DejaVu Sans Mono:style=Bold",halign="center");
}}
module lid_assembled(){difference(){union(){
 slab(body,height-roof_t,roof_t);walls(seam,height-seam);nesting_skirt();
 for(p=axes)translate([p[0],p[1],seam])cylinder(r=boss_r,h=height-seam);
 for(i=[0:3])intersection(){
 p=mounts[i];translate([p[0],p[1],upper_pad])cylinder(d=4.6,h=height-upper_pad);
 // USB-C shell relief retained from the exact-board R4 geometry.
 if(i==1)translate([60.5,0,upper_pad-.1])cube([5,8,20]);
 else translate([-10,-5,0])cube([90,40,25]);
 }
 }ports();outlets();
 for(p=axes){
 // Straight rectangular passage leaves only two parallel bridge shelves.
 translate([p[0]-clearance_d/2,p[1]-head_d/2,seam-.1])cube([clearance_d,head_d,head_land-seam+.2]);
 translate([p[0],p[1],head_land])cylinder(d=head_d,h=height-head_land+.1);
 }
 for(i=[0,3])translate([mounts[i][0],mounts[i][1],upper_pad-.01])cylinder(d=3,h=.6);
 translate([8,15,19.6])linear_extrude(.41)resize([8,0],auto=true)import(brand_asset,center=true);
 translate([29,26.5,19.6])linear_extrude(.41)text("R5 L  < LAN",size=2.4,font="DejaVu Sans Mono:style=Bold",halign="center");
}}
module lid(){translate([0,28.5,20])rotate([180,0,0])lid_assembled();}
module screw(){union(){cylinder(d=3,h=8);translate([0,0,-3])cylinder(d=6,h=3);}}
module insert(){difference(){cylinder(d=insert_od,h=insert_l);translate([0,0,-.01])cylinder(d=3,h=insert_l+.02);}}
module driver(){cylinder(d=3,h=35);}
module heat_tool(){cylinder(d=2.4,h=1);translate([0,0,1])cylinder(d=4.2,h=2);translate([0,0,3])cylinder(d=5,h=27);}
if(part=="base")base();
else if(part=="lid")lid();
else if(part=="lid-assembled")lid_assembled();
else if(part=="screw")screw();
else if(part=="insert")insert();
else if(part=="driver")driver();
else if(part=="heat-tool")heat_tool();
else if(part=="assembled"||part=="exploded"){
 color([.65,.65,.65])base();color([.76,.76,.76])translate([0,0,part=="exploded"?25:0])lid_assembled();
 for(p=axes){color("silver")translate([p[0],p[1],head_land])rotate([180,0,0])screw();color("gold")translate([p[0],p[1],insert_top-insert_l])insert();}
}
else assert(false,"Unknown selector");
