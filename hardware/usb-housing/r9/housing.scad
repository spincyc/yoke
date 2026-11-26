// R9 hardened compact Yoke endpoint housings. Millimetres.
// Frame: x/y are the Radxa ZERO 3E PCB frame (RJ45 toward -x, USB-C toward -y);
// z = 0 is the underside of the base. One base serves both variants.
// Supersedes R8 after physical fit on real ZERO 3E v1.2 hardware proved R8's
// three 6x5 mm front base pads (y=0.8..5.8) collided with underside components
// omitted from Radxa's 3D STEP (U12 SOIC-16, micro-HDMI & USB-C THT legs, ED9-12,
// C151), lifting the front side of the board by several millimetres while the
// rear edge sat flush. Also addresses all first R9 independent cold-review findings:
// - Replaces R8 front interior bumps with three narrow bare-edge pads verified
//   0-hit against official radxa_zero_3e_v1200_bottom.dxf across the full 3-DOF
//   reachable (dx, dy, yaw) workspace, opening 4.20 mm clear floor height under U12;
// - Adds 8 positive PCB-perimeter locator stops on the base (two -X stops at x=-0.25,
//   two +X stops at x=65.25 flanking microSD, two rear +Y stops at y=30.25 on the
//   rear lip bar, and two front -Y stops at y=-0.25 including between USB3 & OTG),
//   constraining PCB travel to dx/dy in [-0.25,+0.25] mm and yaw to [-0.55,+0.48] deg
//   even before the lid is closed;
// - Sets body[3]=34.50 / inside[3]=31.80 (84.55 x 39.20 mm, rear axes y=29.50) so
//   rear pads y=[29.25,30.25] and rear bare_fins y=[29.55,31.80] clear the real
//   official 40-pin GPIO centerline (CON1 y=[24.135,29.215], bottom pads y<=28.771);
// - Relocates bare_fins front rib to [17.8,20.2]x[-2.0,0.80] and closed-end rib to
//   [64.15,72.10]x[14.5,17.5], verified 0-hit against official top DXF (U6, C340-348);
// - Retains all R8 hardened features: uniform 2.70 mm outer walls, zero lid corner
//   seat recess (seam=4.3, lid_bottom=contact_z=4.6), solid perimeter-forced insert
//   bosses (0 sparse-infill layers in G-code), 14.5 mm RA023 HAT spacing, solid
//   shrouding over all 4 live 48-57 V corner mounts and bare 40-pin GPIO, expanded
//   15x10 mm USB-C bay and +1.55 mm top-latch RJ45 window, and recessed roof labels.
part="assembled-poe"; emit_parameters=false; $fn=64;
insert_od=4.6; insert_l=5.7; insert_pilot=4.0;
hat_gap=14.5;          // SBC top to HAT underside (official RA023 M2.5x14.5 pillars)
board_z=7.4;           // SBC top surface; PCB underside board_z-1.6=5.8, resting on pads
floor_t=1.6; roof_t=1.6; poe_roof_clear=3.0; bare_roof_clear=1.7;
body=[-9.75,74.80,-4.70,34.50];   // outer x0,x1,y0,y1 (84.55 x 39.20 mm)
inside=[-7.05,72.10,-2.00,31.80]; // uniform 2.70 mm lid walls; rear lip sits behind PCB y=30.0
seam=4.3; seam_gap=0.3; lid_bottom=seam+seam_gap; // 4.6 = contact_z (no recess!)
lip_clear=0.4; lip_t=1.2; lip_top=5.6; lip_chamfer=0.5;
// Case screw axes: 5.00 mm inset from both adjacent outer faces on all 4 corners
// (dy = 29.50 - 0.30 = 29.20 = 73 x 0.40 mm exact raster grid match; >= 1.62 mm solid
// insert wall inside r=3.92 AND 0.96 mm solid 2-loop exterior skin outside r=3.92..4.04).
axes=[[-4.75,0.30],[-4.75,29.50],[69.80,0.30],[69.80,29.50]];
// Lid post_r=4.05 gives 2.05 mm wall on inboard arc (solid under wall_loops=2 + gap fill)
// while keeping >= 1.75 mm nominal wall outside the 4.6 mm insert OD and >= 34.9 mm2 flat seat.
post_r=4.05; base_post_r=4.25;
// Base post ring stays full diameter-8.50 from floor to flat seat at contact_z=4.6:
// two sacrificial bridge layers over head well, then four solid fused layers.
// Lid post bottom bears directly on the seat at z=4.6 with zero corner wall cut.
head_d=6.3; head_chamfer=0.4; head_land=3.4; clearance_d=3.4;
contact_z=4.6; insert_bottom=contact_z+0.2; pilot_end=12.1;
hat_top=board_z+hat_gap+1.6;       // 23.5 mm
poe_top=board_z+hat_gap+13.7;      // 35.6 mm (tallest official HAT component)
bare_top=board_z+8.5;              // 15.9 mm (GPIO pin tips)
poe_height=poe_top+poe_roof_clear+roof_t;   // 40.2 mm (printed lid 35.6 mm = 178 layers)
bare_height=bare_top+bare_roof_clear+roof_t;// 19.2 mm (printed lid 14.6 mm = 73 layers)
lid_flip_y=body[2]+body[3];
// Radxa conductive mounting axes (live 48-57 V PoE center-taps).
mounts=[[3.549904,3.599942],[61.399928,3.599942],[3.599942,26.450036],[61.399928,26.500074]];
mount_keepout_r=4.1;
// Supported PCB edge pads verified 0-hit against official radxa_zero_3e_v1200_bottom.dxf
// across full 3-DOF reachable (dx, dy, yaw) workspace inside the 8-stop base. Each is [x0,x1,y0,y1]:
// - Front 1 [11.0,13.8]x[-0.45,1.40]: bare belly between micro-HDMI THT legs, fused to front lip.
// - Front 2 [20.2,24.8]x[-0.45,0.85]: bare front edge between M3 & R71 (south of C202), fused to lip.
// - Front 3 [51.0,53.6]x[-0.35,1.00]: bare belly behind USB-OTG left THT leg, solid to floor, 1.25 mm behind USB-C plug sweep.
// - Rear 1..3 y=[29.25,30.25]: bare rear strip >=0.23 mm clear of real GPIO bottom pads (y<=28.771) across full float/yaw.
front_pads=[[11.0,13.8,-0.45,1.40],[20.2,24.8,-0.45,0.85],[51.0,53.6,-0.35,1.00]];
back_pads=[[10.0,16.0,29.25,30.25],[27.0,33.0,29.25,30.25],[46.0,52.0,29.25,30.25]];
// Eight positive PCB-perimeter locator stops on the base (z=0..7.20, 0.20 mm below PCB top):
// constrain PCB to dx/dy in [-0.25,+0.25] mm and yaw in [-0.55,+0.48] deg even with lid off.
pcb_stop_z=board_z-0.2; // 7.20 mm (0.60 mm clear below bare_fins z=7.80)
x_stops_neg=[[-1.85,-0.25,5.45,6.65],[-1.85,-0.25,23.40,24.60]]; // inner extensions of RJ45 cradle walls
x_stops_pos=[[65.25,66.85,6.20,8.60],[65.25,66.85,21.40,23.80]]; // flanking microSD (y=9..21) & mounts
y_stops_front=[[20.6,24.8,-1.40,-0.25],[46.9,48.7,-1.25,-0.25]]; // front lip & USB3/OTG gap (0.35 mm behind plug sweep)
y_stops_rear=[[10.0,16.0,30.25,31.40],[48.0,54.0,30.25,31.40]];  // on rear lip bar (y=30.20..31.40)
// Lid retention ribs stop 0.4 mm above component-free top edges (never clamping).
poe_fins=[[12.0,14.4,inside[2],0.9],[48.0,50.4,inside[2],0.9],
          [12.0,14.4,28.4,inside[3]],[48.0,50.4,28.4,inside[3]]];
// Bare ribs verified 0-hit against official radxa_zero_3e_v1200_top.dxf across full 3-DOF float/yaw:
// - Front rib [17.8,20.2]x[-2.0,0.80]: >=1.87 mm clear of micro-HDMI, >=0.38 mm clear of U6, >=0.56 mm PCB overlap.
// - Closed-end rib [64.15,72.10]x[14.5,17.5]: >=0.30 mm clear of C340-348, >=0.60 mm PCB overlap.
// - Rear ribs y=[29.55,31.80]: clear real CON1 shroud (y<=29.215) across full 3-DOF float/yaw.
bare_fins=[[17.8,20.2,inside[2],0.80],[64.15,inside[1],14.5,17.5],
           [18.0,22.0,29.55,inside[3]],[43.0,47.0,29.55,inside[3]]];
fin_play=0.4;
vent_w=3.0; vent_pitch=5.0; // sturdy 2.0 mm vertical struts
lan_window=[6.03,23.73,board_z-5.05,board_z+8.40];   // y0,y1,z0,z1 (1.55 mm top latch margin)
usb_window=[33.8,61.8,3.7,board_z+6.8];              // x0,x1,z0,z1 (15x10 mm overmold +0.28/-0.22 margin)
low_z=[1.8,3.7];                                     // strictly below base rim seam=4.3
mid_z=[board_z+2.0,board_z+hat_gap-2.2];             // [9.4, 19.7]
mid_over_usb_z=[usb_window[3]+1.6,mid_z[1]];         // [15.8, 19.7]
upper_z=[hat_top+1.8,poe_height-roof_t-1.8];         // [25.3, 36.8]
bare_z=[board_z+1.6,bare_height-roof_t-1.6];         // [9.0, 16.0]
poe_roof_y=[2.0,27.5];
bare_roof_y=[2.0,22.0];                              // shrouds exposed 40-pin GPIO (y>=23.7)
function clear(x,zones,m=1.0)=len([for(z=zones) if(x+vent_w>z[0]-m && x<z[1]+m) 1])==0;
function xs(a,b,zones)=[for(x=[a:vent_pitch:b]) if(clear(x,zones)) x];
// All vents stay strictly in x=10..55, shrouding all 4 live corner mounts (>=6.4 mm away).
// front_low stays strictly clear of front_pads ([11.0,13.8] and [20.2,24.8]) by >=1.7 mm.
front_low=[15.5,26.5];
back_low=[17.5,22.5,34.5,39.5];
poe_front_mid=xs(10.0,29.0,[]);
poe_front_over=xs(35.0,51.0,[]);
poe_upper_front=xs(10.0,51.0,[[12.0,14.4],[48.0,50.4]]);
poe_upper_back=poe_upper_front;
bare_front=[10.0,23.0,28.0];                         // >=2.8 mm clear of front bare rib [17.8,20.2], >=2.0 mm struts
bare_back=xs(10.0,51.0,[[18.0,22.0],[43.0,47.0]]);
roof_x=[for(x=[10:5:50]) x];                         // 9 slots covering x=10..53

function slot_area(list,z)=len(list)*vent_w*(z[1]-z[0]);
if(emit_parameters){
 echo("R9PARAM","outer_xy_mm",[body[1]-body[0],body[3]-body[2]]);
 echo("R9PARAM","poe_envelope_mm",[body[1]-body[0],body[3]-body[2],poe_height]);
 echo("R9PARAM","bare_envelope_mm",[body[1]-body[0],body[3]-body[2],bare_height]);
 echo("R9PARAM","body_bounds_xy_mm",body); echo("R9PARAM","inside_bounds_xy_mm",inside);
 echo("R9PARAM","hat_gap_mm",hat_gap); echo("R9PARAM","board_top_z_mm",board_z);
 echo("R9PARAM","pcb_support_z_mm",board_z-1.6);
 echo("R9PARAM","pcb_stop_z_mm",pcb_stop_z);
 echo("R9PARAM","poe_height_mm",poe_height); echo("R9PARAM","bare_height_mm",bare_height);
 echo("R9PARAM","screw_axes_mm",axes); echo("R9PARAM","screw_length_mm",8);
 echo("R9PARAM","lid_post_diameter_mm",2*post_r); echo("R9PARAM","head_well_diameter_mm",head_d);
 echo("R9PARAM","shank_clearance_diameter_mm",clearance_d); echo("R9PARAM","head_bearing_z_mm",head_land);
 echo("R9PARAM","head_recess_mm",head_land-3); echo("R9PARAM","seam_z_mm",seam);
 echo("R9PARAM","lid_bottom_z_mm",lid_bottom); echo("R9PARAM","seam_gap_mm",seam_gap);
 echo("R9PARAM","base_post_ring_diameter_mm",2*base_post_r);
 echo("R9PARAM","post_contact_z_mm",contact_z);
 echo("R9PARAM","fused_layers_above_bridges",round((contact_z-head_land-0.4)/0.2));
 echo("R9PARAM","insert_bottom_z_mm",insert_bottom); echo("R9PARAM","insert_top_z_mm",insert_bottom+insert_l);
 echo("R9PARAM","pilot_end_z_mm",pilot_end); echo("R9PARAM","pilot_diameter_mm",insert_pilot);
 echo("R9PARAM","insert_diameter_mm",insert_od); echo("R9PARAM","insert_length_mm",insert_l);
 echo("R9PARAM","lip_thickness_mm",lip_t); echo("R9PARAM","lip_top_z_mm",lip_top);
 echo("R9PARAM","lip_engagement_mm",lip_top-lid_bottom);
 echo("R9PARAM","lip_per_side_clearance_mm",lip_clear); echo("R9PARAM","lip_entry_chamfer_mm",lip_chamfer);
 echo("R9PARAM","mount_axes_mm",mounts); echo("R9PARAM","base_mount_keepout_diameter_mm",2*mount_keepout_r);
 echo("R9PARAM","front_pads_xy_mm",front_pads); echo("R9PARAM","back_pads_xy_mm",back_pads);
 echo("R9PARAM","pcb_stops_xy_mm",[x_stops_neg,x_stops_pos,y_stops_front,y_stops_rear]);
 echo("R9PARAM","poe_fins_xy_mm",poe_fins); echo("R9PARAM","poe_fin_bottom_z_mm",hat_top+fin_play);
 echo("R9PARAM","bare_fins_xy_mm",bare_fins); echo("R9PARAM","bare_fin_bottom_z_mm",board_z+fin_play);
 echo("R9PARAM","fin_vertical_play_mm",fin_play);
 echo("R9PARAM","floor_thickness_mm",floor_t); echo("R9PARAM","roof_thickness_mm",roof_t);
 echo("R9PARAM","poe_roof_clearance_mm",poe_roof_clear); echo("R9PARAM","bare_roof_clearance_mm",bare_roof_clear);
 echo("R9PARAM","lan_window_mm",lan_window); echo("R9PARAM","usb_window_mm",usb_window);
 echo("R9PARAM","low_inlet_z_mm",low_z); echo("R9PARAM","poe_vent_z_mm",[mid_z,mid_over_usb_z,upper_z]); echo("R9PARAM","bare_vent_z_mm",bare_z);
 echo("R9PARAM","poe_roof_y_mm",poe_roof_y); echo("R9PARAM","bare_roof_y_mm",bare_roof_y);
 echo("R9PARAM","low_inlet_area_mm2",slot_area(front_low,low_z)+slot_area(back_low,low_z));
 echo("R9PARAM","poe_side_vent_area_mm2",slot_area(poe_front_mid,mid_z)+slot_area(poe_front_over,mid_over_usb_z)
   +slot_area(poe_upper_front,upper_z)+slot_area(poe_upper_back,upper_z));
 echo("R9PARAM","bare_side_vent_area_mm2",slot_area(bare_front,bare_z)+slot_area(bare_back,bare_z));
 echo("R9PARAM","poe_roof_vent_area_mm2",len(roof_x)*vent_w*(poe_roof_y[1]-poe_roof_y[0]));
 echo("R9PARAM","bare_roof_vent_area_mm2",len(roof_x)*vent_w*(bare_roof_y[1]-bare_roof_y[0]));
 echo("R9PARAM","vent_x_mm",[front_low,back_low,poe_front_mid,poe_front_over,poe_upper_front,poe_upper_back,bare_front,bare_back,roof_x]);
 echo("R9PARAM","poe_lid_assembly_matrix",[[1,0,0,0],[0,-1,0,lid_flip_y],[0,0,-1,poe_height],[0,0,0,1]]);
 echo("R9PARAM","bare_lid_assembly_matrix",[[1,0,0,0],[0,-1,0,lid_flip_y],[0,0,-1,bare_height],[0,0,0,1]]);
 echo("R9PARAM","board_matrix",[[1,0,0,0],[0,1,0,0],[0,0,1,board_z],[0,0,0,1]]);
 echo("R9PARAM","poe_reference_top_clearance_mm",poe_height-roof_t-poe_top);
 echo("R9PARAM","bare_reference_top_clearance_mm",bare_height-roof_t-bare_top);
}
module slab(b,z,h){translate([b[0],b[2],z])cube([b[1]-b[0],b[3]-b[2],h]);}
module lan_cut(){translate([body[0]-1,lan_window[0],lan_window[2]])
 cube([inside[0]-body[0]+1.01,lan_window[1]-lan_window[0],lan_window[3]-lan_window[2]]);}
module usb_cut(){translate([usb_window[0],body[2]-1,usb_window[2]])
 cube([usb_window[1]-usb_window[0],inside[2]-body[2]+1.01,usb_window[3]-usb_window[2]]);}
module low_inlets(){
 for(x=front_low)translate([x,body[2]-1,low_z[0]])cube([vent_w,inside[2]+lip_clear+lip_t-body[2]+1.01,low_z[1]-low_z[0]]);
 for(x=back_low)translate([x,inside[3]-lip_clear-lip_t-0.01,low_z[0]])cube([vent_w,body[3]-inside[3]+lip_clear+lip_t+1.01,low_z[1]-low_z[0]]);
}
// Chamfered lip bar along one axis; stays strictly inside mount-free and USB-free spans.
module lip_bar_x(x0,x1,y0,y1,chamfer_side){
 hull(){
  slab([x0,x1,y0,y1],seam,lip_top-seam-lip_chamfer);
  slab([x0,x1,y0+(chamfer_side<0?lip_chamfer:0),y1-(chamfer_side>0?lip_chamfer:0)],seam,lip_top-seam);
 }
}
module lip_bar_y(x0,x1,y0,y1){
 hull(){
  slab([x0,x1,y0,y1],seam,lip_top-seam-lip_chamfer);
  slab([x0,x1-lip_chamfer,y0,y1],seam,lip_top-seam);
 }
}
// Beveled PCB guide tab rising from lip_top to pcb_stop_z with 0.4 mm outer lid-entry bevel.
module y_guide_tab(b,chamfer_side){
 hull(){
  slab(b,0,pcb_stop_z-0.4);
  slab([b[0],b[1],b[2]+(chamfer_side<0?0.4:0),b[3]-(chamfer_side>0?0.4:0)],0,pcb_stop_z);
 }
}
module lip_ring(){
 // Front lip strictly clear of mounts and completely absent across USB-C bay.
 lip_bar_x(10.0,31.5,inside[2]+lip_clear,inside[2]+lip_clear+lip_t,-1);
 // Rear lip strictly clear of both rear 48 V mounts (x=3.6 and 61.4); sits behind PCB y=30.0.
 lip_bar_x(10.0,54.0,inside[3]-lip_clear-lip_t,inside[3]-lip_clear,1);
 // Closed-end lip bar clear of closed-end corner posts and expanded microSD envelope.
 lip_bar_y(inside[1]-lip_clear-lip_t,inside[1]-lip_clear,8.0,22.0);
}
module base(){difference(){union(){
 difference(){slab(body,0,seam);
  slab([inside[0],inside[1]-lip_clear-lip_t,inside[2]+lip_clear+lip_t,inside[3]-lip_clear-lip_t],floor_t,seam);}
 lip_ring();
 for(p=axes)translate([p[0],p[1],0])cylinder(r=base_post_r,h=contact_z);
 for(b=front_pads)slab(b,0,board_z-1.6);
 for(b=back_pads)slab(b,0,board_z-1.6);
 // Eight positive PCB-perimeter locator stops (all solid straight down to floor z=0):
 for(b=x_stops_neg)slab(b,0,pcb_stop_z);
 for(b=x_stops_pos)slab(b,0,pcb_stop_z);
 y_guide_tab(y_stops_front[0],-1);
 slab(y_stops_front[1],0,pcb_stop_z);
 for(b=y_stops_rear)y_guide_tab(b,1);
 // RJ45: low guard 0.4 mm below jack body and two sturdy 1.2 mm side cradle walls
 // tightened to +/-0.27 mm around jack body (y=6.93..23.13), ending at seam=4.3.
 slab([-6.0,-1.0,9.0,21.0],0,board_z-4.55-0.4);
 slab([-6.0,-1.0,5.45,6.65],0,seam);
 slab([-6.0,-1.0,23.40,24.60],0,seam);
 }
 // Trim outside body cleanly.
 slab([body[0]-5,body[1]+5,body[2]-5,body[2]],-1,20); slab([body[0]-5,body[1]+5,body[3],body[3]+5],-1,20);
 slab([body[0]-5,body[0],body[2]-5,body[3]+5],-1,20); slab([body[1],body[1]+5,body[2]-5,body[3]+5],-1,20);
 // Clear base rim behind USB-C opening so overmolded plugs seat with zero lip interference,
 // routed cleanly around the right-front PCB stop y_stops_front[1] ([46.9,48.7]x[-1.25,-0.25]).
 translate([usb_window[0],inside[2],floor_t])cube([y_stops_front[1][0]-usb_window[0],lip_clear+lip_t+0.01,seam-floor_t+0.1]);
 translate([y_stops_front[1][1],inside[2],floor_t])cube([usb_window[1]-y_stops_front[1][1],lip_clear+lip_t+0.01,seam-floor_t+0.1]);
 translate([y_stops_front[1][0]-0.01,inside[2],floor_t])cube([y_stops_front[1][1]-y_stops_front[1][0]+0.02,y_stops_front[1][2]-inside[2],seam-floor_t+0.1]);
 lan_cut(); usb_cut(); low_inlets();
 // Keep diameter-8.2 keepout clear above floor at all 4 live mounting axes.
 for(m=mounts)translate([m[0],m[1],floor_t])cylinder(r=mount_keepout_r,h=20);
 for(p=axes)translate([p[0],p[1],0]){
  translate([0,0,-0.01])cylinder(d1=head_d+2*head_chamfer,d2=head_d,h=head_chamfer+0.01);
  translate([0,0,-0.1])cylinder(d=head_d,h=head_land+0.1);
  // Printed floor-down: a one-layer slot leaves two head-bearing shelves that
  // bridge the well; a one-layer 3.4 square bridges that slot (matching clearance_d);
  // four solid fused layers carry the round shank hole up to the flat seat at z=4.6.
  translate([-clearance_d/2,-head_d/2,head_land-0.01])cube([clearance_d,head_d,0.21]);
  translate([-clearance_d/2,-clearance_d/2,head_land+0.19])cube([clearance_d,clearance_d,0.22]);
  translate([0,0,head_land+0.39])cylinder(d=clearance_d,h=contact_z-head_land);
 }
 translate([32.5,14.9,-0.01])linear_extrude(0.41)mirror([1,0,0])
  text("YOKE R9 BASE  LAN >",size=2.4,font="DejaVu Sans Mono:style=Bold",halign="center",valign="center");
}}
module fins(poe,h){
 zb=poe?hat_top+fin_play:board_z+fin_play;
 for(f=poe?poe_fins:bare_fins)
  translate([f[0],f[2]-(f[2]==inside[2]?0.01:0),zb])
   cube([f[1]-f[0]+(f[1]==inside[1]?0.01:0),f[3]-f[2]+(f[2]==inside[2]||f[3]==inside[3]?0.01:0),h-roof_t-zb+0.01]);
}
module side_vents(poe){
 module row(list,z,front){for(x=list)
  translate([x,front?body[2]-1:inside[3]-0.01,z[0]])cube([vent_w,(front?inside[2]-body[2]+1.01:body[3]-inside[3]+1.01),z[1]-z[0]]);}
 if(poe){row(poe_front_mid,mid_z,true);row(poe_front_over,mid_over_usb_z,true);
  row(poe_upper_front,upper_z,true);row(poe_upper_back,upper_z,false);}
 else{row(bare_front,bare_z,true);row(bare_back,bare_z,false);}
}
// Open-top vertical perimeter-forcing micro-seams outside the r=2.3..3.90 Ruthex
// solid wall zone (r=3.92..4.04) along the exterior corner-wall sectors of each lid post.
// Opening onto the hidden z=contact_z=4.6 seat face keeps each lid a single closed
// two-manifold shell with zero internal cavities or unsupported overhangs, while
// forcing OrcaSlicer (wall_loops=2, 15% infill) to print solid concentric walls + gap
// fill through the full r=2.00..3.92 insert boss across z=4.6..12.6, leaving 0.96 mm
// unbroken 2-loop exterior corner skin, solid radial tie bridges, and >= 34.9 mm2 seat.
module boss_perimeter_seams(p){
 sx=p[0]<30?-1:1;
 sy=p[1]<14?-1:1;
 base_a=atan2(sy,sx); // exterior 45-deg corner bisect angle
 // Three 60-deg arc slits covering the exterior corner sector where the post merges
 // into the 2.70 mm side/end walls, leaving solid radial tie bridges between them.
 for(da=[-78,0,78])
  translate([p[0],p[1],contact_z-0.01])
   rotate([0,0,base_a+da-30])
    rotate_extrude(angle=60,$fn=64)
     translate([3.92,0,0])square([0.12,pilot_end+0.5-(contact_z-0.01)]);
}
module lid_assembled(poe=true){
 h=poe?poe_height:bare_height;
 ry=poe?poe_roof_y:bare_roof_y;
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
  for(p=axes){
   translate([p[0],p[1],0]){
    // Lid post bears flat on base post ring at contact_z=lid_bottom=4.6 (no wall recess).
    translate([0,0,contact_z-0.01])cylinder(d=insert_pilot,h=pilot_end-contact_z+0.01);
    translate([0,0,contact_z-0.01])cylinder(d1=insert_od+0.3,d2=insert_pilot,h=0.46);
   }
   boss_perimeter_seams(p);
  }
  lan_cut();
  usb_cut();
  side_vents(poe);
  for(x=roof_x)translate([x,ry[0],h-roof_t-0.1])cube([vent_w,ry[1]-ry[0],roof_t+0.2]);
  // Roof labels recessed 0.4 mm, inset 1.9 mm from front outer edge and roof vents.
  translate([16.5,-1.0,h-0.4])linear_extrude(0.41)
   text(poe?"YOKE R9 POE <LAN":"YOKE R9 USB <LAN",size=2.1,font="DejaVu Sans Mono:style=Bold",halign="center",valign="center");
  translate([41.6,-1.0,h-0.4])linear_extrude(0.41)
   text("USB3",size=2.0,font="DejaVu Sans Mono:style=Bold",halign="center",valign="center");
  translate([54.0,-1.0,h-0.4])linear_extrude(0.41)
   text("OTG",size=2.0,font="DejaVu Sans Mono:style=Bold",halign="center",valign="center");
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
