#import <ApplicationServices/ApplicationServices.h>
#import <Cocoa/Cocoa.h>
#import <ImageIO/ImageIO.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#import <UniformTypeIdentifiers/UniformTypeIdentifiers.h>

static CGRect activeDisplayUnion(void) {
    uint32_t count = 0;
    if (CGGetActiveDisplayList(0, NULL, &count) != kCGErrorSuccess || count == 0) {
        return CGRectNull;
    }
    CGDirectDisplayID *displays = calloc(count, sizeof(CGDirectDisplayID));
    if (!displays) return CGRectNull;
    if (CGGetActiveDisplayList(count, displays, &count) != kCGErrorSuccess) {
        free(displays);
        return CGRectNull;
    }
    CGRect result = CGRectNull;
    for (uint32_t index = 0; index < count; index++) {
        CGRect displayBounds = CGDisplayBounds(displays[index]);
        result = CGRectIsNull(result) ? displayBounds : CGRectUnion(result, displayBounds);
    }
    free(displays);
    return result;
}

static CGImageRef createRegionImage(
    CGFloat x,
    CGFloat y,
    CGFloat width,
    CGFloat height,
    CGRect *capturedRectOut
) {
    CGRect requested = CGRectMake(x, y, width, height);
    CGRect displayUnion = activeDisplayUnion();
    CGRect rect = CGRectIsNull(displayUnion)
        ? requested
        : CGRectIntersection(requested, displayUnion);
    if (CGRectIsNull(rect) || CGRectIsEmpty(rect)) {
        fprintf(stderr, "capture_outside_displays\n");
        return NULL;
    }
    if (capturedRectOut) *capturedRectOut = rect;
    dispatch_semaphore_t semaphore = dispatch_semaphore_create(0);
    __block CGImageRef image = NULL;
    __block NSError *captureError = nil;
    [SCScreenshotManager captureImageInRect:rect completionHandler:^(CGImageRef result, NSError *error) {
        if (result) image = CGImageRetain(result);
        captureError = error;
        dispatch_semaphore_signal(semaphore);
    }];
    if (dispatch_semaphore_wait(semaphore, dispatch_time(DISPATCH_TIME_NOW, 12 * NSEC_PER_SEC)) != 0) {
        fprintf(stderr, "capture_timeout\n");
        return NULL;
    }
    if (!image) {
        fprintf(stderr, "capture_failed: %s\n", captureError.localizedDescription.UTF8String ?: "请检查屏幕录制权限");
        return NULL;
    }
    return image;
}

static NSData *encodePNG(CGImageRef image) {
    NSMutableData *data = [NSMutableData data];
    CGImageDestinationRef destination = CGImageDestinationCreateWithData(
        (__bridge CFMutableDataRef)data,
        (__bridge CFStringRef)UTTypePNG.identifier,
        1,
        NULL
    );
    if (!destination) return nil;
    CGImageDestinationAddImage(destination, image, NULL);
    bool ok = CGImageDestinationFinalize(destination);
    CFRelease(destination);
    return ok ? data : nil;
}

static NSData *renderBGRA(CGImageRef image, size_t *widthOut, size_t *heightOut, size_t *bytesPerRowOut) {
    size_t width = CGImageGetWidth(image);
    size_t height = CGImageGetHeight(image);
    size_t bytesPerRow = width * 4;
    NSMutableData *data = [NSMutableData dataWithLength:bytesPerRow * height];
    CGColorSpaceRef colorSpace = CGColorSpaceCreateDeviceRGB();
    CGContextRef context = CGBitmapContextCreate(
        data.mutableBytes,
        width,
        height,
        8,
        bytesPerRow,
        colorSpace,
        kCGImageAlphaPremultipliedFirst | kCGBitmapByteOrder32Little
    );
    CGColorSpaceRelease(colorSpace);
    if (!context) return nil;
    // SCScreenshotManager's CGImage already draws into bitmap row zero in the
    // same top-to-bottom order as ImageIO's PNG output. Applying the usual
    // Quartz flip here would invert every board coordinate vertically.
    CGContextDrawImage(context, CGRectMake(0, 0, width, height), image);
    CGContextRelease(context);
    *widthOut = width;
    *heightOut = height;
    *bytesPerRowOut = bytesPerRow;
    return data;
}

static int captureRegion(CGFloat x, CGFloat y, CGFloat width, CGFloat height, NSString *path) {
    CGRect capturedRect = CGRectZero;
    CGImageRef image = createRegionImage(x, y, width, height, &capturedRect);
    if (!image) return 3;
    NSURL *url = [NSURL fileURLWithPath:path];
    CGImageDestinationRef destination = CGImageDestinationCreateWithURL(
        (__bridge CFURLRef)url,
        (__bridge CFStringRef)UTTypePNG.identifier,
        1,
        NULL
    );
    if (!destination) {
        CGImageRelease(image);
        fprintf(stderr, "cannot_create_png\n");
        return 4;
    }
    CGImageDestinationAddImage(destination, image, NULL);
    bool ok = CGImageDestinationFinalize(destination);
    CFRelease(destination);
    CGImageRelease(image);
    return ok ? 0 : 5;
}

static int serveCaptures(void) {
    // Keep the signed ScreenCaptureKit process alive and return raw BGRA bytes
    // directly over stdout. PNG compression/decompression previously consumed
    // a large part of every live-analysis frame.
    setvbuf(stdout, NULL, _IONBF, 0);
    char line[512];
    while (fgets(line, sizeof(line), stdin)) {
        @autoreleasepool {
            double x = 0, y = 0, width = 0, height = 0;
            if (sscanf(line, "%lf %lf %lf %lf", &x, &y, &width, &height) != 4) {
                fprintf(stdout, "0\n");
                continue;
            }
            CGRect capturedRect = CGRectZero;
            CGImageRef image = createRegionImage(x, y, width, height, &capturedRect);
            if (!image) {
                fprintf(stdout, "0\n");
                continue;
            }
            size_t pixelWidth = 0, pixelHeight = 0, bytesPerRow = 0;
            NSData *data = renderBGRA(image, &pixelWidth, &pixelHeight, &bytesPerRow);
            CGImageRelease(image);
            if (!data) {
                fprintf(stdout, "0\n");
                continue;
            }
            fprintf(
                stdout,
                "RAW %zu %zu %zu %lu %.6f %.6f %.6f %.6f\n",
                pixelWidth,
                pixelHeight,
                bytesPerRow,
                (unsigned long)data.length,
                capturedRect.origin.x,
                capturedRect.origin.y,
                capturedRect.size.width,
                capturedRect.size.height
            );
            fwrite(data.bytes, 1, data.length, stdout);
        }
    }
    return 0;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 2) {
            fprintf(stderr, "usage: screen-tool capture x y width height output.png | serve | permissions\n");
            return 2;
        }
        NSString *command = [NSString stringWithUTF8String:argv[1]];
        if ([command isEqualToString:@"capture"] && argc == 7) {
            return captureRegion(atof(argv[2]), atof(argv[3]), atof(argv[4]), atof(argv[5]),
                                 [NSString stringWithUTF8String:argv[6]]);
        }
        if ([command isEqualToString:@"serve"]) {
            return serveCaptures();
        }
        if ([command isEqualToString:@"permissions"]) {
            bool shouldRequest = argc >= 3 && strcmp(argv[2], "request") == 0;
            bool screen = shouldRequest ? CGRequestScreenCaptureAccess() : CGPreflightScreenCaptureAccess();
            printf("{\"screen\":%s}\n", screen ? "true" : "false");
            return screen ? 0 : 1;
        }
        fprintf(stderr, "invalid_arguments\n");
        return 2;
    }
}
