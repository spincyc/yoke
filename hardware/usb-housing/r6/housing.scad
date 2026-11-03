// R6: ZERO 3E + PoE HAT. mm. Board-edge retention is independent of HAT height.
part="assembled"; emit_parameters=false; $fn=64;
insert_od=4.6; insert_l=5.7; insert_pilot=4.0;
height=46; floor_t=2; roof_t=2; seam=13.5;
body=[-9.2,79.8,-11,34.5]; inside=[-7.2,77.8,-9,32.5];
axes=[[-5,1.6],[-5,28],[75.6,1.6],[75.6,28]]; boss_r=4.2;
head_d=6.8; head_land=3.6; clearance_d=4.0;
insert_bottom=seam; pilot_end=21.2; board_z=8.8;
lip_t=1.2; lip_depth=2; lip_clearance=.5; lip_chamfer=.5;
clip_axes=[[23.5,-4.3],[72.5,15.4]];
clip_boss_top=8; clip_top=11; clip_head_land=10; clip_toe_z=9.2;
clip_insert_bottom=2.3; clip_pilot_bottom=1.2;
pads=[[20,26,0,1.8],[48,54,0,1.8],[17,23,28.8,30],[45,51,28.8,30]];
// Bounds are references; allowance cylinders are deliberately larger than nominal hardware.
mounts=[[3.549904,3.599942],[61.399928,3.599942],[3.599942,26.450036],[61.399928,26.500074]];
vent_x=[for(x=[8:4.5:62])x]; roof_x=[for(x=[8:5:63])x];
brand_asset="../../../site/assets/yoke-mark.svg";

if(emit_parameters){
 echo("R6PARAM","envelope_mm",[body[1]-body[0],body[3]-body[2],height]);
 echo("R6PARAM","body_bounds_xy_mm",body); echo("R6PARAM","inside_bounds_xy_mm",inside);
 echo("R6PARAM","screw_axes_mm",axes); echo("R6PARAM","screw_length_mm",16);
 echo("R6PARAM","boss_diameter_mm",2*boss_r); echo("R6PARAM","head_well_diameter_mm",head_d);
 echo("R6PARAM","shank_passage_width_mm",clearance_d); echo("R6PARAM","head_bearing_z_mm",head_land);
 echo("R6PARAM","head_recess_mm",head_land-3); echo("R6PARAM","seam_z_mm",seam);
 echo("R6PARAM","insert_bottom_z_mm",insert_bottom); echo("R6PARAM","insert_top_z_mm",insert_bottom+insert_l);
 echo("R6PARAM","pilot_end_z_mm",pilot_end); echo("R6PARAM","pilot_diameter_mm",insert_pilot);
 echo("R6PARAM","insert_diameter_mm",insert_od); echo("R6PARAM","insert_length_mm",insert_l);
 echo("R6PARAM","lip_thickness_mm",lip_t); echo("R6PARAM","lip_depth_mm",lip_depth);
 echo("R6PARAM","lip_per_side_clearance_mm",lip_clearance); echo("R6PARAM","lip_entry_chamfer_mm",lip_chamfer);
 echo("R6PARAM","pcb_pads_xy_mm",pads); echo("R6PARAM","pcb_lower_pad_z_mm",board_z-1.6);
 echo("R6PARAM","pcb_upper_capture_z_mm",clip_toe_z); echo("R6PARAM","clip_axes_mm",clip_axes);
 echo("R6PARAM","clip_boss_top_z_mm",clip_boss_top); echo("R6PARAM","clip_top_z_mm",clip_top);
 echo("R6PARAM","clip_head_bearing_z_mm",clip_head_land); echo("R6PARAM","clip_screw_length_mm",8);
 echo("R6PARAM","clip_insert_bottom_z_mm",clip_insert_bottom); echo("R6PARAM","clip_pilot_bottom_z_mm",clip_pilot_bottom);
 echo("R6PARAM","interboard_side_vent_area_mm2",2*len(vent_x)*3*12);
 echo("R6PARAM","upper_side_vent_area_mm2",2*len(vent_x)*3*8);
 echo("R6PARAM","top_vent_area_mm2",len(roof_x)*3*33);
 echo("R6PARAM","floor_thickness_mm",floor_t); echo("R6PARAM","roof_thickness_mm",roof_t);
 echo("R6PARAM","lid_assembly_matrix",[[1,0,0,0],[0,-1,0,23.5],[0,0,-1,height],[0,0,0,1]]);
 echo("R6PARAM","clip_assembly_matrix",[[1,0,0,0],[0,-1,0,0],[0,0,-1,clip_top],[0,0,0,1]]);
 echo("R6PARAM","board_matrix",[[1,0,0,0],[0,1,0,0],[0,0,1,board_z],[0,0,0,1]]);
 echo("R6PARAM","reference_top_clearance_mm",height-roof_t-(board_z+31.7));
 echo("R6PARAM","lan_window_mm",[-10.2,6,3.0,5.4,18,15.5]);
 echo("R6PARAM","usb_window_mm",[34.7,-12,6.3,26.2,12.8,8.2]);
}
module slab(b,z,h){translate([b[0],b[2],z])cube([b[1]-b[0],b[3]-b[2],h]);}
module walls(z,h){difference(){slab(body,z,h);slab(inside,z-.01,h+.02);}}
module ports(){
 // Ethernet end only. Opposite end is closed; microSD is serviced with lid off.
 translate([body[0]-1,6,3])cube([-4.8-body[0]+1,18,15.5]);
 // Two USB-C sockets share one low, narrow collar tunnel. No HDMI aperture.
 translate([34.7,body[2]-1,6.3])cube([26.2,.8-body[2]+1,8.2]);
}
module side_vents(){
 for(x=vent_x)for(z_h=[[15.5,12],[32.5,8]]){
  translate([x,body[2]-1,z_h[0]])cube([3,8,z_h[1]]);
  translate([x,inside[3]-3,z_h[0]])cube([3,7,z_h[1]]);
 }
 // Low side vents serve the underside even when the floor rests on a desk.
 for(x=[10:5:60]){
  translate([x,body[2]-1,2.8])cube([3,4,2.4]);
  translate([x,inside[3]-.1,2.8])cube([3,4,2.4]);
 }
}
module roof_vents(){for(x=roof_x)translate([x,-3,height-roof_t-.1])cube([3,33,roof_t+.2]);}
module skirt_profile(){polygon([[lip_chamfer,seam-lip_depth],[lip_t,seam-lip_depth],
 [lip_t,height-roof_t+.02],[0,height-roof_t+.02],[0,seam-lip_depth+lip_chamfer]]);}
module skirt(){difference(){union(){
 for(back=[false,true])translate([inside[0]+lip_clearance,back?inside[3]-lip_clearance:inside[2]+lip_clearance,0])
  scale([1,back?-1:1,1])rotate([90,0,90])linear_extrude(inside[1]-inside[0]-2*lip_clearance)skirt_profile();
 for(right=[false,true])translate([right?inside[1]-lip_clearance:inside[0]+lip_clearance,inside[3]-lip_clearance,0])
  scale([right?-1:1,1,1])rotate([90,0,0])linear_extrude(inside[3]-inside[2]-2*lip_clearance)skirt_profile();
 }
 for(p=axes)translate([p[0],p[1],0])cylinder(r=boss_r+lip_clearance,h=seam+.01);
 for(p=clip_axes)translate([p[0],p[1],0])cylinder(r=4.7,h=14.0);
}}
module base(){difference(){union(){
 slab(body,0,floor_t); walls(0,seam);
 for(p=axes)translate([p[0],p[1],0])cylinder(r=boss_r,h=seam);
 for(p=clip_axes)translate([p[0],p[1],0])cylinder(r=boss_r,h=clip_boss_top);
 for(b=pads)slab(b,0,board_z-1.6);
 // Side guides stop board drift while leaving mounting holes and components clear.
 for(b=[[28.5,34.5,-1.1,-.3],[48,54,-1.1,-.3],[17,23,30.3,31.2],[45,51,30.3,31.2]])slab(b,0,9.8);
 // End guide at clear PCB edge; no pad underneath the microSD socket.
 slab([65.3,66.3,7,8.5],0,9.0);slab([65.3,66.3,22,23.5],0,9.0);
 slab([-4,-.3,23.9,25.3],0,9.8);
 // A low guard limits downward jack deflection without locating on its metal.
 slab([-2.8,.5,8,22],0,3.2);
 }ports();side_vents();
 for(p=axes){
  translate([p[0],p[1],-.1])cylinder(d=head_d,h=head_land+.1);
  translate([p[0]-clearance_d/2,p[1]-head_d/2,head_land-.01])cube([clearance_d,head_d,seam-head_land+.1]);
 }
 for(p=clip_axes)translate([p[0],p[1],clip_pilot_bottom]){
  cylinder(d=insert_pilot,h=clip_boss_top-clip_pilot_bottom+.1);
  translate([0,0,clip_boss_top-clip_pilot_bottom-.35])cylinder(d1=insert_pilot,d2=insert_od+.3,h=.36);
 }
 translate([30,15,-.01])linear_extrude(.41)mirror([1,0,0])text("R6 B  LAN >",size=2.4,font="DejaVu Sans Mono:style=Bold",halign="center");
}}
module lid_assembled(){difference(){union(){
 slab(body,height-roof_t,roof_t);walls(seam,height-seam);skirt();
 for(p=axes)translate([p[0],p[1],seam])cylinder(r=boss_r,h=height-seam);
 }ports();side_vents();roof_vents();
 for(p=axes)translate([p[0],p[1],seam-.01]){
  cylinder(d=insert_pilot,h=pilot_end-seam+.01);
  cylinder(d1=insert_od+.3,d2=insert_pilot,h=.36);
 }
 translate([30,-6.7,height-.4])linear_extrude(.41)text("YOKE  R6  < LAN",size=2.4,font="DejaVu Sans Mono:style=Bold",halign="center");
}}
module clip_assembled(right=false){
 p=clip_axes[right?1:0];
 difference(){union(){
  translate([p[0]-4.2,p[1]-4.2,clip_boss_top])cube([8.4,8.4,clip_top-clip_boss_top]);
  if(right)translate([63.5,12.4,clip_toe_z])cube([p[0]-63.5,6,clip_top-clip_toe_z]);
  else translate([20,-4.3,clip_toe_z])cube([7,5.7,clip_top-clip_toe_z]);
 }
 translate([p[0],p[1],clip_head_land])cylinder(d=head_d,h=2);
 translate([p[0]-clearance_d/2,p[1]-head_d/2,clip_boss_top-.01])cube([clearance_d,head_d,clip_head_land-clip_boss_top+.02]);
 }
}
module lid(){translate([0,23.5,height])rotate([180,0,0])lid_assembled();}
module clip(right=false){translate([0,0,clip_top])rotate([180,0,0])clip_assembled(right);}
// Hardware origins: screw bearing plane z0, shaft +Z, head -Z.
module screw(l=16){union(){cylinder(d=3,h=l);translate([0,0,-3])cylinder(d=6,h=3);}}
module insert(){difference(){cylinder(d=insert_od,h=insert_l);translate([0,0,-.01])cylinder(d=3,h=insert_l+.02);}}
module driver(){cylinder(d=3,h=35);}
module heat_tool(){cylinder(d=2.4,h=1);translate([0,0,1])cylinder(d=4.2,h=2);translate([0,0,3])cylinder(d=5,h=27);}
if(part=="base")base();
else if(part=="lid")lid();
else if(part=="lid-assembled")lid_assembled();
else if(part=="front-clip")clip();
else if(part=="right-clip")clip(true);
else if(part=="screw")screw();
else if(part=="clip-screw")screw(8);
else if(part=="insert")insert();
else if(part=="driver")driver();
else if(part=="heat-tool")heat_tool();
else if(part=="assembled"||part=="exploded"){
 color([.65,.65,.65])base();color([.76,.76,.76])translate([0,0,part=="exploded"?40:0])lid_assembled();
 color("orange"){clip_assembled();clip_assembled(true);}
}
else assert(false,"Unknown selector");
