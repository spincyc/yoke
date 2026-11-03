// Yoke ZERO 3E housing R4. Native mm CAD; final insert pilot is deliberately unset.
// A preview is an engineering layout, never a print release.
part="assembled";
provisional=false;
insert_od=4.6; insert_l=5.7; insert_pilot=-1;
$fn=64;
assert(provisional || insert_pilot>0,"Insert OD/length and pilot authority required; use provisional=true only for layout review");
pilot=insert_pilot>0?insert_pilot:4.0; // Ruthex catalogue profile; stock identity unconfirmed
height=20; floor_t=2; roof_t=2; seam=13.5;
body=[-9.2,67.6,-3.7,32.2]; inside=[-7.4,65.4,-1.9,30.4];
axes=[[-5.0,1.6],[-5.0,28.0]]; boss_r=4.2;
head_d=6.4; head_land=16.5; clearance_d=3.4;
insert_top=13.5; blind_bottom=6.8;
slide=1.2; pcb_top=8.5; lower_pad=6.88; upper_pad=8.70;
mounts=[[3.549904,3.599942],[61.399928,3.599942],[3.599942,26.450036],[61.399928,26.500074]];
keys=[[16,30.6,12],[47,30.6,12],[1.5,-2.1,4]];
key_w=1.0; key_h=1.2; key_c=0.25;
brand_asset="../../../site/assets/yoke-mark.svg";
module slab(b,z,h){translate([b[0],b[2],z])cube([b[1]-b[0],b[3]-b[2],h]);}
module walls(z,h){difference(){slab(body,z,h);slab(inside,z-.01,h+.02);}}
module ports(){
 translate([body[0]-1,6.1,2.9])cube([-6.8-body[0]+1,17.9,13.3]);
 translate([6.8,body[2]-1,5.8])cube([53.7,-1.8-body[2]+1,9]);
 translate([65.3,7,3.5])cube([body[1]-65.3+1,16,10.2]);
}
module vents(z){for(x=[16:6:52])translate([x,8,z])cube([3,13,4]);}
module base_key(k){translate([k[0]-k[2]/2,k[1]-key_w/2,seam]){
 cube([k[2],key_w,key_h-.3]);
 hull(){translate([0,0,key_h-.3])cube([k[2],key_w,.01]);translate([.25,.25,key_h-.01])cube([k[2]-.5,key_w-.5,.01]);}
}}
module key_slot(k){
 // Entry is 1.2 mm left of final position; the elongated slots guide the slide.
 translate([k[0]-k[2]/2-key_c,k[1]-key_w/2-key_c,seam-.01])
 cube([k[2]+slide+2*key_c,key_w+2*key_c,key_h+.30]);
}
module rear_receiver(){for(y=[0,24]){
 // A real roof over an inward-facing slot. Integral rear wall takes lip load.
 translate([65.4,y,seam])cube([2.2,6,3]);
}}
module rear_slot_cut(){for(y=[0,24]){
 // Match the tab's print chamfer with0.30 vertical running clearance;
 // the full inclined face shares opening load, rather than a tiny flat toe.
 translate([0,y+5.2,0])rotate([90,0,0])linear_extrude(4.4)
 polygon([[65.35,13.49],[66.4,13.49],[66.4,14.5],[66.15,14.5],
          [65.55,15],[65.35,15]]);
}}

module rear_tab(){for(y=[1.05,25.05]){
 // Single native profile: no tangent union facets. Chamfer prints roof-down.
 // Full lip underlap is0.75; thickness1.0 at root,0.5 at outer tip.
 translate([0,y+3.9,0])rotate([90,0,0])linear_extrude(3.9)
 polygon([[63.7,13.7],[66.15,13.7],[66.15,14.2],[65.55,14.7],
          [65.2,14.7],[65.2,18.1],[63.7,18.1]]);
}}
module locator(i){p=mounts[i];translate([p[0],p[1],lower_pad])intersection(){
 union(){cylinder(d=2.35,h=1.67);translate([0,0,1.67])cylinder(d1=2.35,d2=1.85,h=.25);}
 if(i==3)rotate([0,0,atan2(mounts[3][1]-mounts[0][1],mounts[3][0]-mounts[0][0])])translate([-.8,-2,0])cube([1.6,4,3]);
 else cylinder(d=3,h=3);
}}
module base(){difference(){union(){
 slab(body,0,floor_t);walls(0,seam);rear_receiver();
 for(p=axes)translate([p[0],p[1],0])cylinder(r=boss_r,h=insert_top);
 for(p=mounts)translate([p[0],p[1],0])cylinder(d=4.6,h=lower_pad);
 for(k=keys)base_key(k);locator(0);locator(3);
 }ports();vents(-1);rear_slot_cut();
 for(p=axes)translate([p[0],p[1],blind_bottom]){
 cylinder(d=pilot,h=insert_top-blind_bottom+.1);
 translate([0,0,insert_top-blind_bottom-.35])cylinder(d1=pilot,d2=insert_od+.3,h=.36);
 }
 translate([28,28,-.01])linear_extrude(.4)mirror([1,0,0])text("R4 B",size=2.4,font="DejaVu Sans Mono:style=Bold",halign="center");
}}
module lid_assembled(){difference(){union(){
 slab(body,height-roof_t,roof_t);
 difference(){walls(seam+.15,height-seam-.15);
 for(y=[-.25,23.75])translate([65.15,y,seam-.01])cube([2.6,6.5,3.3]);}
 for(p=axes)translate([p[0],p[1],seam])cylinder(r=boss_r,h=height-seam);
 // Local inward pads leave0.85mm real walls on both sides of every guide slot.
 for(k=keys)translate([k[0]-k[2]/2-1.1,k[1]-1.6,seam+.15])
 cube([k[2]+slide+2.2,3.2,height-roof_t-seam-.15]);
 for(i=[0:3])intersection(){
 p=mounts[i];translate([p[0],p[1],upper_pad])cylinder(d=4.6,h=height-upper_pad);
 // Front-right pad is relieved to clear the USB-C shell during 1.2 mm slide.
 if(i==1)translate([60.5,0,upper_pad-.1])cube([5,8,20]);
 else translate([-10,-5,0])cube([80,40,25]);
 }
 rear_tab();
 }ports();vents(height-3);
 for(k=keys)key_slot(k);
 // Receiver relief is cut from wall before unioning the retaining tabs.
 for(p=axes){
 // A 3.4 X x 6.4 Y capsule passage leaves two flat bridge bearing shelves.
 // Unlike an annular counterbore roof, each shelf bridges onto two supports.
 hull()for(dy=[-1.5,1.5])translate([p[0],p[1]+dy,seam-.1])cylinder(d=clearance_d,h=head_land-seam+.2);
 translate([p[0],p[1],head_land])cylinder(d=head_d,h=height-head_land+.1);}
 for(i=[0,3])hull()for(dx=[0,slide])translate([mounts[i][0]+dx,mounts[i][1],upper_pad-.01])cylinder(d=3,h=.6);
 translate([9,15,19.55])linear_extrude(.46)resize([8,0],auto=true)import(brand_asset,center=true);
 translate([28,28,19.55])linear_extrude(.46)text("R4 L",size=2.4,font="DejaVu Sans Mono:style=Bold",halign="center");
}}
module lid(){translate([0,28.5,20])rotate([180,0,0])lid_assembled();}
module coupon(which){
 // Intersect native parts; critical lands, passages, guides and hook are unchanged.
 // The lower front sample includes a real floor ledge for its clamp.
 bounds=which=="front-base"?[-9.2,20,-3.7,9.1]:
        which=="front"?[-9.2,8,-3.7,6.3]:[55.6,67.6,-2.1,9.1];
 intersection(){children();slab(bounds,-.1,20.2);}
}
module rear_jig(){
 // Fixture, not housing: mechanically restrains X while allowing rear lid Z lift.
 // A 5 mm stop reduces expected flex under the 5 N trial; measure it in use.
 difference(){union(){
 slab([50.5,70.6,-18.5,11.5],0,2);
 translate([50.5,-3.1,2])cube([5.0,13.2,22]);
 translate([67.7,-3.1,2])cube([1.4,13.2,3]);
 for(y=[-3.1,9.4])translate([55.5,y,2])cube([12.2,.7,3]);
 }
 // A low rear opening admits the RB floor hold-down without touching RL.
 translate([55.5,9.3,2])cube([8.8,.9,4]);
 }
}
module screw(){union(){cylinder(d=3,h=8);translate([0,0,-3])cylinder(d=6,h=3);}}
module insert(){difference(){cylinder(d=insert_od,h=insert_l);translate([0,0,-.01])cylinder(d=3,h=insert_l+.02);}}
module driver(){cylinder(d=3,h=35);}
module heat_tool(){cylinder(d=2.4,h=1);translate([0,0,1])cylinder(d=4.2,h=2);translate([0,0,3])cylinder(d=5,h=27);}
if(part=="base")base();
else if(part=="lid")lid();
else if(part=="lid-assembled")lid_assembled();
else if(part=="front-base")coupon("front-base")base();
else if(part=="front-lid")translate([0,28.5,20])rotate([180,0,0])coupon("front")lid_assembled();
else if(part=="rear-base")coupon("rear")base();
else if(part=="rear-lid")translate([0,28.5,20])rotate([180,0,0])coupon("rear")lid_assembled();
else if(part=="rear-jig")rear_jig();
else if(part=="screw")screw();
else if(part=="insert")insert();
else if(part=="driver")driver();
else if(part=="heat-tool")heat_tool();
else if(part=="plate"){base();translate([0,47,0])lid();}
else if(part=="assembled"||part=="exploded"){
 color([.65,.65,.65])base();color([.76,.76,.76])translate([0,0,part=="exploded"?25:0])lid_assembled();
 for(p=axes){color("silver")translate([p[0],p[1],head_land])rotate([180,0,0])screw();color("gold")translate([p[0],p[1],insert_top-insert_l])insert();}
}
else assert(false,"Unknown selector");
