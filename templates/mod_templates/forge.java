package com.example.examplemod;

import net.minecraftforge.common.MinecraftForge;
import net.minecraftforge.event.RegisterCommandsEvent;
import net.minecraftforge.event.entity.player.PlayerEvent;
import net.minecraftforge.eventbus.api.IEventBus;
import net.minecraftforge.eventbus.api.SubscribeEvent;
import net.minecraftforge.fml.common.Mod;
import net.minecraftforge.fml.event.lifecycle.FMLCommonSetupEvent;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.commands.Commands;
import net.minecraft.network.chat.Component;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import com.mojang.brigadier.Command;

// Forge 1.21 (Forge 51.x): constructor MUST accept IEventBus — FMLJavaModLoadingContext is REMOVED.
@Mod(ExampleMod.MOD_ID)
public class ExampleMod {
    public static final String MOD_ID = "examplemod";
    private static final Logger LOGGER = LogManager.getLogger();

    // IEventBus is injected by Forge — accept it as a constructor parameter.
    // NEVER use FMLJavaModLoadingContext.get().getModEventBus() — it does not exist in 1.21.
    public ExampleMod(IEventBus modEventBus) {
        modEventBus.addListener(this::setup);
        MinecraftForge.EVENT_BUS.register(this);
        LOGGER.info("ExampleMod constructed.");
    }

    private void setup(FMLCommonSetupEvent event) {
        LOGGER.info("ExampleMod common setup complete.");
    }

    @SubscribeEvent
    public void onRegisterCommands(RegisterCommandsEvent event) {
        event.getDispatcher().register(
            Commands.literal("example")
                .executes(ctx -> {
                    ctx.getSource().sendSuccess(
                        () -> Component.literal("ExampleMod is working!"),
                        false
                    );
                    return Command.SINGLE_SUCCESS;
                })
        );
    }

    @SubscribeEvent
    public void onPlayerLoggedIn(PlayerEvent.PlayerLoggedInEvent event) {
        event.getEntity().sendSystemMessage(
            Component.literal("Welcome to the server!")
        );
    }
}
