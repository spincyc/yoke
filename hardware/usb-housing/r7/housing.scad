// R7 compact Yoke endpoint housings. Millimetres.
// Frame: x/y are the Radxa ZERO 3E PCB frame (RJ45 toward -x, USB-C toward -y);
// z = 0 is the underside of the base. One base serves both variants.
// The base never depends on HAT height; only the lid (hat_gap) does.
part="assembled-poe"; emit_parameters=false; $fn=64;
insert_od=4.6; insert_l=5.7; insert_pilot=4.0;
hat_gap=18;            // SBC top to HAT underside (official CAD); lid-only parameter
board_z=7.4;           // SBC top surface; PCB underside board_z-1.6, resting on pads
floor_t=1.6; roof_t=1.6; roof_clear=1.0;
body=[-8.1,73.3,-3.65,32.6];      // outer x0,x1,y0,y1
inside=[-6.8,71.3,-1.65,30.6];    // lid inner faces
seam=3.6; seam_gap=0.3; lid_bottom=seam+seam_gap;
lip_clear=0.4; lip_t=1.2; lip_top=5.4; lip_chamfer=0.5;
// Case screw axes: two beside the RJ45, two closed-end corner posts merged
// into the end and side walls just outside the board stack's lowering sweep.
axes=[[-4.2,1.2],[-4.2,28.6],[69.3,0.3],[69.3,28.6]];
post_r=3.9; base_post_r=3.9; seat_clear=0.4;
// Each base post stays a full diameter-7.8 ring from the floor to the seat at
// contact_z: two sacrificial bridge layers over the head well, then four fused
// layers. The lid post annulus bears on that seat (the only hard stop).
head_d=6.2; head_land=3.4; clearance_d=3.4;
contact_z=4.6; insert_bottom=contact_z+0.2; pilot_end=12.1;
hat_top=board_z+hat_gap+1.6;
poe_top=board_z+hat_gap+13.7;      // tallest official HAT component
bare_top=board_z+8.5;              // GPIO pin tips
poe_height=poe_top+roof_clear+roof_t;
bare_height=bare_top+roof_clear+roof_t;
lid_flip_y=body[2]+body[3];
// Supported PCB edge regions (component-free in the official CAD).
pad_x=[[10,16],[27,33],[46,52]];
// Radxa conductive mounting axes; the base keeps a diameter-8.2 keepout above its floor.
mounts=[[3.549904,3.599942],[61.399928,3.599942],[3.599942,26.450036],[61.399928,26.500074]];
mount_keepout_r=4.1;
front_pad_y=[0.8,5.8];
back_pad_y=[inside[3]-lip_clear-lip_t,inside[3]-lip_clear];
// Lid ribs bear on component-free top edges with 0.4 nominal vertical play.
poe_fins=[[12,14.4,inside[2],0.9],[48,50.4,inside[2],0.9],
          [12,14.4,28.4,inside[3]],[48,50.4,28.4,inside[3]]];
// Rear bare ribs sit on bare PCB between the hole pads and the GPIO header.
bare_fins=[[16.2,18.2,inside[2],0.9],[29,31,inside[2],0.9],
           [5.95,6.85,29.5,inside[3]],[58.15,59.05,29.5,inside[3]]];
fin_play=0.4;
vent_w=3;
lan_window=[6.43,23.63,board_z-5.05,board_z+7.35];   // y0,y1,z0,z1
usb_window=[34.8,60.8,board_z+5.7];                  // x0,x1,ztop (open below)
low_z=[2.1,5.6];
// Lid vent rows follow the stack height so a hat_gap change regenerates only the lid.
mid_z=[board_z+2.1,board_z+hat_gap-2.4]; mid_over_usb_z=[usb_window[2]+1.9,mid_z[1]];
upper_z=[hat_top+1.5,poe_height-roof_t-1.6];
bare_z=[board_z+1.6,bare_height-roof_t-1.5];
roof_y=[2.0,27.0];
function clear(x,zones,m=0.8)=len([for(z=zones) if(x+vent_w>z[0]-m && x<z[1]+m) 1])==0;
function xs(a,b,zones)=[for(x=[a:4.5:b]) if(clear(x,zones)) x];
// Low inlets stay between the edge pads so no pad face blocks an inlet.
front_low=[1.5,6,17,21.5,34,38.5,42.5,53,57.5,62];
back_low=front_low;
poe_front_mid=xs(1.5,31.3,[]);
poe_front_over=xs(35.5,57.8,[]);
// The HAT's female GPIO header (x 6.8-61.7) stands 1.4 mm behind the rear
// wall between the boards, so only its two clear ends get rear slots.
poe_back_mid=[1.5,62.2];
poe_upper=xs(1.5,60.5,[[12,14.4],[48,50.4]]);
bare_front=xs(1.5,31.3,[[16.2,18.2],[29,31]]);
bare_back=xs(1.5,60.5,[[5.95,6.85],[58.15,59.05]]);
roof_x=[for(x=[2:5:62]) x];

function slot_area(list,z)=len(list)*vent_w*(z[1]-z[0]);
if(emit_parameters){
 echo("R7PARAM","outer_xy_mm",[body[1]-body[0],body[3]-body[2]]);
 echo("R7PARAM","poe_envelope_mm",[body[1]-body[0],body[3]-body[2],poe_height]);
 echo("R7PARAM","bare_envelope_mm",[body[1]-body[0],body[3]-body[2],bare_height]);
 echo("R7PARAM","body_bounds_xy_mm",body); echo("R7PARAM","inside_bounds_xy_mm",inside);
 echo("R7PARAM","hat_gap_mm",hat_gap); echo("R7PARAM","board_top_z_mm",board_z);
 echo("R7PARAM","pcb_support_z_mm",board_z-1.6);
 echo("R7PARAM","poe_height_mm",poe_height); echo("R7PARAM","bare_height_mm",bare_height);
 echo("R7PARAM","screw_axes_mm",axes); echo("R7PARAM","screw_length_mm",8);
 echo("R7PARAM","lid_post_diameter_mm",2*post_r); echo("R7PARAM","head_well_diameter_mm",head_d);
 echo("R7PARAM","shank_clearance_diameter_mm",clearance_d); echo("R7PARAM","head_bearing_z_mm",head_land);
 echo("R7PARAM","head_recess_mm",head_land-3); echo("R7PARAM","seam_z_mm",seam);
 echo("R7PARAM","lid_bottom_z_mm",lid_bottom); echo("R7PARAM","seam_gap_mm",seam_gap);
 echo("R7PARAM","base_post_ring_diameter_mm",2*base_post_r); echo("R7PARAM","lid_seat_recess_diameter_mm",2*(post_r+seat_clear));
 echo("R7PARAM","post_contact_z_mm",contact_z);
 echo("R7PARAM","fused_layers_above_bridges",round((contact_z-head_land-0.4)/0.2));
 echo("R7PARAM","insert_bottom_z_mm",insert_bottom); echo("R7PARAM","insert_top_z_mm",insert_bottom+insert_l);
 echo("R7PARAM","pilot_end_z_mm",pilot_end); echo("R7PARAM","pilot_diameter_mm",insert_pilot);
 echo("R7PARAM","insert_diameter_mm",insert_od); echo("R7PARAM","insert_length_mm",insert_l);
 echo("R7PARAM","lip_thickness_mm",lip_t); echo("R7PARAM","lip_top_z_mm",lip_top);
 echo("R7PARAM","lip_engagement_mm",lip_top-lid_bottom);
 echo("R7PARAM","lip_per_side_clearance_mm",lip_clear); echo("R7PARAM","lip_entry_chamfer_mm",lip_chamfer);
 echo("R7PARAM","mount_axes_mm",mounts); echo("R7PARAM","base_mount_keepout_diameter_mm",2*mount_keepout_r);
 echo("R7PARAM","pad_x_mm",pad_x); echo("R7PARAM","front_pad_y_mm",front_pad_y); echo("R7PARAM","back_pad_y_mm",back_pad_y);
 echo("R7PARAM","poe_fins_xy_mm",poe_fins); echo("R7PARAM","poe_fin_bottom_z_mm",hat_top+fin_play);
 echo("R7PARAM","bare_fins_xy_mm",bare_fins); echo("R7PARAM","bare_fin_bottom_z_mm",board_z+fin_play);
 echo("R7PARAM","fin_vertical_play_mm",fin_play);
 echo("R7PARAM","floor_thickness_mm",floor_t); echo("R7PARAM","roof_thickness_mm",roof_t);
 echo("R7PARAM","poe_roof_clearance_mm",roof_clear); echo("R7PARAM","bare_roof_clearance_mm",roof_clear);
 echo("R7PARAM","lan_window_mm",lan_window); echo("R7PARAM","usb_window_mm",usb_window);
 echo("R7PARAM","low_inlet_z_mm",low_z); echo("R7PARAM","poe_vent_z_mm",[mid_z,mid_over_usb_z,upper_z]); echo("R7PARAM","bare_vent_z_mm",bare_z);
 echo("R7PARAM","low_inlet_area_mm2",slot_area(front_low,low_z)+slot_area(back_low,low_z));
 echo("R7PARAM","poe_side_vent_area_mm2",slot_area(poe_front_mid,mid_z)+slot_area(poe_front_over,mid_over_usb_z)
   +slot_area(poe_back_mid,mid_z)+2*slot_area(poe_upper,upper_z));
 echo("R7PARAM","bare_side_vent_area_mm2",slot_area(bare_front,bare_z)+slot_area(bare_back,bare_z));
 echo("R7PARAM","roof_vent_area_mm2",len(roof_x)*vent_w*(roof_y[1]-roof_y[0]));
 echo("R7PARAM","vent_x_mm",[front_low,back_low,poe_front_mid,poe_front_over,poe_back_mid,poe_upper,bare_front,bare_back,roof_x]);
 echo("R7PARAM","poe_lid_assembly_matrix",[[1,0,0,0],[0,-1,0,lid_flip_y],[0,0,-1,poe_height],[0,0,0,1]]);
 echo("R7PARAM","bare_lid_assembly_matrix",[[1,0,0,0],[0,-1,0,lid_flip_y],[0,0,-1,bare_height],[0,0,0,1]]);
 echo("R7PARAM","board_matrix",[[1,0,0,0],[0,1,0,0],[0,0,1,board_z],[0,0,0,1]]);
 echo("R7PARAM","poe_reference_top_clearance_mm",poe_height-roof_t-poe_top);
 echo("R7PARAM","bare_reference_top_clearance_mm",bare_height-roof_t-bare_top);
}
module slab(b,z,h){translate([b[0],b[2],z])cube([b[1]-b[0],b[3]-b[2],h]);}
module lan_cut(){translate([body[0]-1,lan_window[0],lan_window[2]])
 cube([inside[0]-body[0]+1.01,lan_window[1]-lan_window[0],lan_window[3]-lan_window[2]]);}
module low_inlets(){
 for(x=front_low)translate([x,body[2]-1,low_z[0]])cube([vent_w,inside[2]+lip_clear+lip_t-body[2]+1.01,low_z[1]-low_z[0]]);
 for(x=back_low)translate([x,inside[3]-lip_clear-lip_t-0.01,low_z[0]])cube([vent_w,body[3]-inside[3]+lip_clear+lip_t+1.01,low_z[1]-low_z[0]]);
}
module lip_ring(){
 o=[inside[0]+lip_clear,inside[1]-lip_clear,inside[2]+lip_clear,inside[3]-lip_clear];
 i=[-20,o[1]-lip_t,o[2]+lip_t,o[3]-lip_t];
 difference(){
  hull(){slab(o,seam,lip_top-seam-lip_chamfer);
   slab([o[0]+lip_chamfer,o[1]-lip_chamfer,o[2]+lip_chamfer,o[3]-lip_chamfer],seam,lip_top-seam);}
  slab(i,seam-1,lip_top);
  slab([-20,inside[0]+lip_clear+0.5,-20,60],seam-1,lip_top);   // no lip at the RJ45 end
  for(p=axes)translate([p[0],p[1],seam-1])cylinder(r=post_r+lip_clear,h=lip_top);
 }
}
module base(){difference(){union(){
 difference(){slab(body,0,seam);
  slab([inside[0],inside[1]-lip_clear-lip_t,inside[2]+lip_clear+lip_t,inside[3]-lip_clear-lip_t],floor_t,seam);}
 lip_ring();
 for(p=axes)translate([p[0],p[1],0])cylinder(r=base_post_r,h=contact_z);
 for(x=pad_x){slab([x[0],x[1],front_pad_y[0],front_pad_y[1]],0,board_z-1.6);
  slab([x[0],x[1],back_pad_y[0],back_pad_y[1]],0,board_z-1.6);}
 // RJ45: low guard 0.4 below the jack body and two side cradle walls.
 slab([-6.0,-1.0,9,21],0,board_z-4.55-0.4);
 slab([-6.0,-1.0,5.9,6.5],0,5.0); slab([-6.0,-1.0,23.56,24.16],0,5.0);
 }
 slab([body[0]-5,body[1]+5,body[2]-5,body[2]],-1,20); slab([body[0]-5,body[1]+5,body[3],body[3]+5],-1,20);
 slab([body[0]-5,body[0],body[2]-5,body[3]+5],-1,20); slab([body[1],body[1]+5,body[2]-5,body[3]+5],-1,20);
 lan_cut(); low_inlets();
 for(m=mounts)translate([m[0],m[1],floor_t])cylinder(r=mount_keepout_r,h=20);
 for(p=axes)translate([p[0],p[1],0]){
  translate([0,0,-0.1])cylinder(d=head_d,h=head_land+0.1);
  // Printed floor-down: a one-layer slot leaves two head-bearing shelves that
  // bridge the well between its walls; a one-layer 3.2 square bridges that
  // slot; the round shank hole starts on the third layer.
  translate([-clearance_d/2,-head_d/2,head_land-0.01])cube([clearance_d,head_d,0.21]);
  translate([-1.6,-1.6,head_land+0.19])cube([3.2,3.2,0.22]);
  translate([0,0,head_land+0.39])cylinder(d=clearance_d,h=contact_z-head_land);
 }
 translate([32.6,14.4,-0.01])linear_extrude(0.41)mirror([1,0,0])
  text("YOKE R7 BASE  LAN >",size=2.6,font="DejaVu Sans Mono:style=Bold",halign="center",valign="center");
}}
module fins(poe,h){
 zb=poe?hat_top+fin_play:board_z+fin_play;
 for(f=poe?poe_fins:bare_fins)
  translate([f[0],f[2]-(f[2]==inside[2]?0.01:0),zb])
   cube([f[1]-f[0],f[3]-f[2]+0.01,h-roof_t-zb+0.01]);
}
module side_vents(poe){
 module row(list,z,front){for(x=list)
  translate([x,front?body[2]-1:inside[3]-0.01,z[0]])cube([vent_w,(front?inside[2]-body[2]+1.01:body[3]-inside[3]+1.01),z[1]-z[0]]);}
 if(poe){row(poe_front_mid,mid_z,true);row(poe_front_over,mid_over_usb_z,true);
  row(poe_back_mid,mid_z,false);row(poe_upper,upper_z,true);row(poe_upper,upper_z,false);}
 else{row(bare_front,bare_z,true);row(bare_back,bare_z,false);}
}
module lid_assembled(poe=true){
 h=poe?poe_height:bare_height;
 difference(){
  intersection(){
   union(){
    slab(body,h-roof_t,roof_t);
    difference(){slab(body,lid_bottom,h-lid_bottom);slab(inside,lid_bottom-0.01,h);}
    for(p=axes)translate([p[0],p[1],lid_bottom])cylinder(r=post_r,h=h-lid_bottom);
    fins(poe,h);
   }
   slab(body,0,h);
  }
  for(p=axes)translate([p[0],p[1],0]){
   // Seat recess: the base post ring rises into it and stops the lid here.
   translate([0,0,lid_bottom-1])cylinder(r=post_r+seat_clear,h=contact_z-lid_bottom+1);
   translate([0,0,contact_z-0.01])cylinder(d=insert_pilot,h=pilot_end-contact_z+0.01);
   translate([0,0,contact_z-0.01])cylinder(d1=insert_od+0.3,d2=insert_pilot,h=0.46);
  }
  lan_cut();
  translate([usb_window[0],body[2]-1,lid_bottom-1])cube([usb_window[1]-usb_window[0],inside[2]-body[2]+1.01,usb_window[2]-lid_bottom+1]);
  low_inlets(); side_vents(poe);
  for(x=roof_x)translate([x,roof_y[0],h-roof_t-0.1])cube([vent_w,roof_y[1]-roof_y[0],roof_t+0.2]);
  translate([32.6,-2.45,h-0.4])linear_extrude(0.41)
   text(poe?"YOKE R7 POE  < LAN":"YOKE R7 USB  < LAN",size=2.4,font="DejaVu Sans Mono:style=Bold",halign="center",valign="center");
 }
}
module lid(poe=true){translate([0,lid_flip_y,poe?poe_height:bare_height])rotate([180,0,0])lid_assembled(poe);}
// Hardware origins: screw bearing plane z0, shaft +Z, head -Z.
module screw(l=8){union(){cylinder(d=3,h=l);translate([0,0,-3])cylinder(d=6,h=3);}}
module insert(){difference(){cylinder(d=insert_od,h=insert_l);translate([0,0,-.01])cylinder(d=3,h=insert_l+.02);}}
module driver(){cylinder(d=3,h=35);}
module heat_tool(){cylinder(d=2.4,h=1);translate([0,0,1])cylinder(d=4.2,h=2);translate([0,0,3])cylinder(d=5,h=27);}
if(part=="base")base();
else if(part=="poe-lid")lid(true);
else if(part=="bare-lid")lid(false);
else if(part=="poe-lid-assembled")lid_assembled(true);
else if(part=="bare-lid-assembled")lid_assembled(false);
else if(part=="screw")screw();
else if(part=="insert")insert();
else if(part=="driver")driver();
else if(part=="heat-tool")heat_tool();
else if(part=="assembled-poe"||part=="exploded-poe"){
 color([.65,.65,.65])base();color([.78,.78,.78])translate([0,0,part=="exploded-poe"?45:0])lid_assembled(true);}
else if(part=="assembled-bare"||part=="exploded-bare"){
 color([.65,.65,.65])base();color([.78,.78,.78])translate([0,0,part=="exploded-bare"?25:0])lid_assembled(false);}
else assert(false,"Unknown selector");
