#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

static FILE *lg(void){ static FILE*f; if(!f) f=fopen("/tmp/ukshim/props.log","a"); return f; }

int *__errno(void){ return __errno_location(); }
static FILE _sf[3];
FILE *__sF = _sf;
void android_set_abort_message(const char *m){ FILE*f=lg(); if(f){fprintf(f,"[abort] %s\n",m?m:"");fflush(f);} }

static const char *PROPS[] = {
    "ro.product.model","ro.product.brand","ro.product.manufacturer","ro.product.device",
    "ro.build.version.sdk","ro.build.version.release","ro.build.version.incremental",
    "ro.build.fingerprint","ro.build.display.id","ro.serialno","ro.boot.serialno",
    "ro.product.cpu.abi","ro.product.cpu.abi2","ro.hardware","ro.board.platform",
    "gsm.sim.operator.numeric","gsm.operator.numeric","gsm.sim.operator.iso-country",
    "persist.sys.timezone","ro.kernel.qemu","ro.debuggable","ro.secure",
    "ro.build.type","ro.build.tags","ro.product.name","ro.build.characteristics",
    "ro.vivo.product.model","ro.vivo.os.version","ro.mediatek.platform",
    "wifi.interface","ro.ril.miui.imei","ro.miui.ui.version.name","ro.build.host",
};
static const char *VALS[] = {
    "vivo V2307A","vivo","vivo","PD2307",
    "36","16","UP1A.231005.007",
    "vivo/V2307A/PD2307:16/UP1A.231005.007/10221800:user/release-keys","V2307A",
    "","", "arm64-v8a","","qcom","mt6893",
    "","","", "Asia/Shanghai","0","0","1",
    "user","release-keys","PD2307","default",
    "v2307a","16.1.0","","wlan0","","","buildhost",
};

int __system_property_get(const char *name, char *value){
    FILE *f=lg(); if(f){fprintf(f,"get %s\n",name?name:"");fflush(f);}
    if(!value) return 0;
    value[0]=0;
    if(!name) return 0;
    for(unsigned i=0;i<sizeof(PROPS)/sizeof(PROPS[0]);i++){
        if(PROPS[i] && !strcmp(PROPS[i],name)){
            const char *v=VALS[i];
            if(!v[0]) return 0;
            strncpy(value,v,91); value[91]=0;
            return (int)strlen(value);
        }
    }
    return 0;
}
const void *__system_property_find(const char*n){ FILE*f=lg(); if(f){fprintf(f,"find %s\n",n?n:"");fflush(f);} return 0; }
int __system_property_read(const void*p,char*n,char*v){ (void)p;(void)n;(void)v; return 0; }
